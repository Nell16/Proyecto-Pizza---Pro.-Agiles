"""
Historia #6 — Modificación de pedidos
Cubre:
- El admin puede modificar pedidos SOLO dentro de 5 minutos posteriores a la confirmación.
- Cambios se reflejan inmediatamente en la pantalla de cocina y en la "vista" del cliente.
- Notificación al cliente simulada (cola/event-bus).
- Concurrencia: cola prioritaria para aplicar múltiples modificaciones sin conflictos.
- Logs con códigos de estado y trazabilidad (MOD_OK, TIME_EXPIRED, SYNC_FAIL, MOD_FAIL).
- Métricas: latencia de modificación y conteo por estado.

Requisitos previos:
- Comparte la misma BD SQLite `data/app.db` creada en la Historia 1.
- Tabla `orders` con columnas: id, client_name, product, size, qty, payment_method, state, created_at, confirmed_at.

Arquitectura:
- OrderRepository: acceso SQLite transaccional (UPDATE atómico).
- ModEvent (DTO): describe la modificación solicitada.
- ModService: valida ventana 5 min, aplica cambios con backoff si hay contención, emite notificaciones.
- KitchenBus / ClientBus: "buses" (colas) para simular reflejo en cocina y notificación al cliente.
- UI: pestañas de (1) Buscar/Editar, (2) Cocina, (3) Cliente, (4) Métricas & Cola.
"""

import os
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import time
import threading
import queue
import uuid
import random

import tkinter as tk
from tkinter import ttk, messagebox

# =========================
# Rutas / Config
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
MOD_LOG = os.path.join(LOGS_DIR, "modificaciones.log")

# Ventana de edición
EDIT_WINDOW_SECONDS = 5 * 60  # 5 minutos

# Backoff para reintento (contienda simulada)
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.25

# Prioridad: 0 (urgente) primero; a igualdad, menor qty primero
# Tema (editable después)
THEME = {"accent": "#1b5e20", "warn": "#e65100", "ok": "#2e7d32", "bad": "#b71c1c"}

# =========================
# Logging
# =========================
logger = logging.getLogger("story6_mods")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = RotatingFileHandler(MOD_LOG, maxBytes=512_000, backupCount=2)
    fmt = logging.Formatter("[%(asctime)s] %(message)s")
    h.setFormatter(fmt)
    logger.addHandler(h)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def to_ts(dt_str: Optional[str]) -> Optional[float]:
    if not dt_str:
        return None
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").timestamp()

# =========================
# DTO de modificación
# =========================
@dataclass(order=True)
class ModEvent:
    # sort_index ordena por prioridad y tamaño
    sort_index: Tuple[int, int] = field(init=False, repr=False)
    urgent: bool
    order_id: int
    new_client_name: Optional[str] = None
    new_product: Optional[str] = None
    new_size: Optional[str] = None
    new_qty: Optional[int] = None
    new_payment_method: Optional[str] = None
    req_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: str = field(default_factory=now_str)

    def __post_init__(self):
        qty = self.new_qty if isinstance(self.new_qty, int) and self.new_qty > 0 else 9999
        prio = 0 if self.urgent else 1
        self.sort_index = (prio, qty)

# =========================
# Repositorio (SQLite)
# =========================
class OrderRepository:
    def __init__(self, path: str):
        self.path = path
        self._ensure_schema()

    def _con(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit

    def _ensure_schema(self):
        with self._con() as con:
            cur = con.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                product TEXT NOT NULL,
                size TEXT NOT NULL,
                qty INTEGER NOT NULL CHECK(qty > 0),
                payment_method TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            )
            """)
            con.commit()

    def fetch_one(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self._con() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, client_name, product, size, qty, payment_method, state, created_at, confirmed_at
                FROM orders WHERE id=?
            """, (order_id,))
            r = cur.fetchone()
            if not r:
                return None
            keys = ["id","client_name","product","size","qty","payment_method","state","created_at","confirmed_at"]
            return dict(zip(keys, r))

    def list_recent(self, limit=50) -> List[Dict[str, Any]]:
        with self._con() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, client_name, product, size, qty, payment_method, state, created_at, confirmed_at
                FROM orders
                ORDER BY id DESC LIMIT ?
            """, (limit,))
            keys = ["id","client_name","product","size","qty","payment_method","state","created_at","confirmed_at"]
            return [dict(zip(keys, r)) for r in cur.fetchall()]

    def apply_modification_atomic(self, order_id: int, changes: Dict[str, Any]) -> bool:
        """
        UPDATE atómico con verificación de ventana de tiempo.
        Retorna True si se aplicó, False si no.
        """
        with self._con() as con:
            cur = con.cursor()
            cur.execute("BEGIN EXCLUSIVE")
            cur.execute("""
                SELECT id, state, confirmed_at FROM orders WHERE id=?
            """, (order_id,))
            row = cur.fetchone()
            if not row:
                con.rollback()
                return False

            _, state, confirmed_at = row
            # Solo permitir si confirmado y dentro de 5 min
            if not confirmed_at:
                con.rollback()
                return False
            ts = to_ts(confirmed_at)
            if ts is None or (time.time() - ts) > EDIT_WINDOW_SECONDS:
                con.rollback()
                return False
            if state in ("cancelled", "cooking", "done"):
                con.rollback()
                return False

            # Construir UPDATE dinámico
            sets = []
            params = []
            for field in ("client_name","product","size","qty","payment_method"):
                if field in changes and changes[field] is not None:
                    sets.append(f"{field}=?")
                    params.append(changes[field])
            if not sets:
                con.rollback()
                return False

            params.append(order_id)
            cur.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", tuple(params))
            con.commit()
            return True

# =========================
# Buses de notificación
# =========================
class KitchenBus:
    """Simula la pantalla de cocina reaccionando a modificaciones."""
    def __init__(self, ui_append):
        self.ui_append = ui_append

    def notify_update(self, order_id: int, changes: Dict[str, Any]):
        msg = f"🍕 [Cocina] Pedido #{order_id} ACTUALIZADO → {changes}"
        self.ui_append(msg)

class ClientBus:
    """Simula notificación al cliente."""
    def __init__(self, ui_append):
        self.ui_append = ui_append

    def notify_update(self, order_id: int, changes: Dict[str, Any]):
        msg = f"📲 [Cliente] Tu pedido #{order_id} fue actualizado: {changes}"
        self.ui_append(msg)

# =========================
# Servicio de Modificaciones
# =========================
class ModService:
    """
    Consume una cola prioritaria de ModEvent y aplica cambios de forma atómica,
    validando la ventana de 5 minutos y notificando a cocina/cliente.
    """
    def __init__(self, repo: OrderRepository, kitchen: KitchenBus, client: ClientBus, ui_metrics_update):
        self.repo = repo
        self.kitchen = kitchen
        self.client = client
        self.ui_metrics_update = ui_metrics_update

        self.pq: "queue.PriorityQueue[ModEvent]" = queue.PriorityQueue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.metrics = {
            "MOD_OK": 0,
            "TIME_EXPIRED": 0,
            "SYNC_FAIL": 0,
            "MOD_FAIL": 0,
            "avg_latency_ms": 0.0,
            "processed": 0
        }
        self._worker.start()

    def stop(self):
        self._stop.set()
        try:
            self.pq.put_nowait(ModEvent(urgent=False, order_id=-1))  # señal
        except Exception:
            pass

    def enqueue(self, ev: ModEvent):
        self.pq.put(ev)

    def _update_latency(self, ms: float):
        n = self.metrics["processed"]
        avg = self.metrics["avg_latency_ms"]
        self.metrics["avg_latency_ms"] = (avg * n + ms) / (n + 1)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                ev = self.pq.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev.order_id == -1:
                break

            t0 = time.time()
            code = "MOD_FAIL"
            info = {}

            # Construir cambios válidos
            changes: Dict[str, Any] = {}
            if ev.new_client_name:   changes["client_name"] = ev.new_client_name.strip()
            if ev.new_product:       changes["product"] = ev.new_product.strip()
            if ev.new_size:          changes["size"] = ev.new_size.strip()
            if isinstance(ev.new_qty, int) and ev.new_qty > 0:
                changes["qty"] = int(ev.new_qty)
            if ev.new_payment_method: changes["payment_method"] = ev.new_payment_method.strip()

            # Validación básica local
            valid_products = ("pepperoni","hawaiana")
            valid_sizes = ("personal","mediana","grande")
            valid_pay = ("efectivo","tarjeta","transferencia")
            if "product" in changes and changes["product"] not in valid_products:
                code = "MOD_FAIL"
                info["reason"] = "invalid_product"
                self._finish(ev, code, t0, info)
                continue
            if "size" in changes and changes["size"] not in valid_sizes:
                code = "MOD_FAIL"; info["reason"] = "invalid_size"; self._finish(ev, code, t0, info); continue
            if "payment_method" in changes and changes["payment_method"] not in valid_pay:
                code = "MOD_FAIL"; info["reason"] = "invalid_payment"; self._finish(ev, code, t0, info); continue

            # Reintentos (contención simulada)
            backoff = INITIAL_BACKOFF_S
            applied = False
            for attempt in range(1, MAX_RETRIES+1):
                ok = self.repo.apply_modification_atomic(ev.order_id, changes)
                if ok:
                    applied = True
                    break
                # distinguir expiración de tiempo vs otros
                current = self.repo.fetch_one(ev.order_id)
                if not current or not current.get("confirmed_at"):
                    code = "MOD_FAIL"; info["reason"] = "not_found_or_unconfirmed"
                    applied = False
                    break
                ts = to_ts(current["confirmed_at"])
                if ts is None or (time.time() - ts) > EDIT_WINDOW_SECONDS:
                    code = "TIME_EXPIRED"; info["reason"] = "edit_window_passed"
                    applied = False
                    break
                # contienda / lock → reintentar
                time.sleep(backoff)
                backoff *= 2

            if not applied:
                if code != "TIME_EXPIRED":
                    code = "MOD_FAIL"
                self._finish(ev, code, t0, info)
                continue

            # Notificar buses; si fallaran, marcamos SYNC_FAIL, pero la modificación ya quedó persistida
            code = "MOD_OK"
            info["changes"] = changes
            sync_ok = True
            try:
                self.kitchen.notify_update(ev.order_id, changes)
            except Exception:
                sync_ok = False
            try:
                self.client.notify_update(ev.order_id, changes)
            except Exception:
                sync_ok = False
            if not sync_ok:
                code = "SYNC_FAIL"

            self._finish(ev, code, t0, info)

    def _finish(self, ev: ModEvent, code: str, t0: float, info: Dict[str, Any]):
        latency_ms = (time.time() - t0) * 1000.0
        self.metrics["processed"] += 1
        self.metrics[code] = self.metrics.get(code, 0) + 1
        self._update_latency(latency_ms)

        # Log estructurado
        log_obj = {
            "tx_id": ev.req_id,
            "order_id": ev.order_id,
            "urgent": ev.urgent,
            "requested_at": ev.requested_at,
            "finished_at": now_str(),
            "code": code,
            "latency_ms": round(latency_ms, 1),
            "info": info
        }
        logger.info(str(log_obj).replace("'", '"'))  # JSON-like

        # Empujar métricas a la UI
        self.ui_metrics_update(self.metrics)

# =========================
# UI
# =========================
class Story6App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #6 — Modificación de pedidos (ventana 5 min, sincronización Cocina/Cliente)")
        self.geometry("1200x780")
        self.minsize(1100, 700)

        self.repo = OrderRepository(DB_PATH)
        self._build_ui()

        # Buses
        self._append_kitchen_safe = lambda msg: self.after(0, lambda: self.kitchen_list.insert("end", msg))
        self._append_client_safe = lambda msg: self.after(0, lambda: self.client_list.insert("end", msg))
        self.kitchen = KitchenBus(self._append_kitchen_safe)
        self.client = ClientBus(self._append_client_safe)

        # Servicio de modificaciones
        self.mod_service = ModService(self.repo, self.kitchen, self.client, self._update_metrics)

        self._refresh_orders()

    def destroy(self):
        try:
            self.mod_service.stop()
        finally:
            return super().destroy()

    # ---------- UI build ----------
    def _build_ui(self):
        nb = ttk.Notebook(self)
        self.tab_edit = ttk.Frame(nb)
        self.tab_kitchen = ttk.Frame(nb)
        self.tab_client = ttk.Frame(nb)
        self.tab_metrics = ttk.Frame(nb)
        nb.add(self.tab_edit, text="Buscar / Editar pedido")
        nb.add(self.tab_kitchen, text="Cocina")
        nb.add(self.tab_client, text="Cliente")
        nb.add(self.tab_metrics, text="Métricas & Cola")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_edit(self.tab_edit)
        self._build_tab_kitchen(self.tab_kitchen)
        self._build_tab_client(self.tab_client)
        self._build_tab_metrics(self.tab_metrics)

        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w", foreground=THEME["accent"])
        status_bar.pack(fill="x", side="bottom")

    def _build_tab_edit(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        # Tabla izquierda: pedidos recientes
        left = ttk.LabelFrame(frm, text="Pedidos recientes", padding=10)
        left.pack(side="left", fill="both", expand=True)

        cols = ("id","cliente","producto","tamano","cant","pago","estado","creado","confirmado")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=22)
        headers = {
            "id":"ID","cliente":"Cliente","producto":"Producto","tamano":"Tamaño","cant":"Cant.",
            "pago":"Pago","estado":"Estado","creado":"Creado","confirmado":"Confirmado"
        }
        widths = {"id":70,"cliente":140,"producto":110,"tamano":90,"cant":70,"pago":120,"estado":90,"creado":160,"confirmado":160}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="center" if c not in ("cliente",) else "w")
        self.tree.pack(fill="both", expand=True)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(8,0))
        ttk.Button(bar, text="Actualizar", command=self._refresh_orders).pack(side="left")
        ttk.Button(bar, text="Cargar selección", command=self._load_selection).pack(side="left", padx=(8,0))

        # Panel derecho: editor + envío a cola prioritaria
        right = ttk.LabelFrame(frm, text="Editor de pedido (válido solo dentro de 5 minutos tras confirmación)", padding=10)
        right.pack(side="left", fill="y", padx=(10,0))

        self.lbl_sel = ttk.Label(right, text="Pedido seleccionado: —")
        self.lbl_sel.grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(right, text="Cliente").grid(row=1, column=0, sticky="w")
        self.ent_client = ttk.Entry(right, width=24)
        self.ent_client.grid(row=1, column=1, sticky="w")

        ttk.Label(right, text="Producto").grid(row=2, column=0, sticky="w")
        self.cmb_product = ttk.Combobox(right, values=["pepperoni","hawaiana"], state="readonly", width=22)
        self.cmb_product.grid(row=2, column=1, sticky="w")

        ttk.Label(right, text="Tamaño").grid(row=3, column=0, sticky="w")
        self.cmb_size = ttk.Combobox(right, values=["personal","mediana","grande"], state="readonly", width=22)
        self.cmb_size.grid(row=3, column=1, sticky="w")

        ttk.Label(right, text="Cantidad").grid(row=4, column=0, sticky="w")
        self.spn_qty = tk.Spinbox(right, from_=1, to=100, width=6)
        self.spn_qty.grid(row=4, column=1, sticky="w")

        ttk.Label(right, text="Pago").grid(row=5, column=0, sticky="w")
        self.cmb_pay = ttk.Combobox(right, values=["efectivo","tarjeta","transferencia"], state="readonly", width=22)
        self.cmb_pay.grid(row=5, column=1, sticky="w")

        self.var_urgent = tk.IntVar(value=0)
        ttk.Checkbutton(right, text="Urgente (alta prioridad)", variable=self.var_urgent).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8,0))

        ttk.Button(right, text="Enviar modificación", command=self._enqueue_mod).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10,4))
        self.lbl_hint = ttk.Label(right, text=f"⚠ Solo dentro de 5 minutos tras Confirmado.\nEvidencias en {MOD_LOG}", foreground="#616161")
        self.lbl_hint.grid(row=8, column=0, columnspan=2, sticky="w")

    def _build_tab_kitchen(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Pantalla de Cocina (Reflejo inmediato de cambios)", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0,8))
        self.kitchen_list = tk.Listbox(frm, height=20)
        self.kitchen_list.pack(fill="both", expand=True)

    def _build_tab_client(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Notificaciones al Cliente", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0,8))
        self.client_list = tk.Listbox(frm, height=20)
        self.client_list.pack(fill="both", expand=True)

    def _build_tab_metrics(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm); top.pack(fill="x")
        self.lbl_metrics = ttk.Label(top, text="Métricas: —")
        self.lbl_metrics.pack(side="left")

        ttk.Button(top, text="Simular 5 modificaciones concurrentes", command=self._simulate_concurrent).pack(side="right")

        mid = ttk.LabelFrame(frm, text="Cola prioritaria (próximas modificaciones)", padding=8)
        mid.pack(fill="both", expand=True, pady=(10,0))
        self.queue_list = tk.Listbox(mid, height=12)
        self.queue_list.pack(fill="both", expand=True)

    # ---------- Acciones ----------
    def _refresh_orders(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        for o in self.repo.list_recent():
            self.tree.insert("", "end", values=(
                o["id"], o["client_name"], o["product"], o["size"], o["qty"],
                o["payment_method"], o["state"], o["created_at"], o["confirmed_at"]
            ))

    def _load_selection(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Selección", "Selecciona un pedido.")
            return
        row = self.tree.item(sel[0])["values"]
        oid, client, product, size, qty, pay, state, created, confirmed = row
        self.lbl_sel.configure(text=f"Pedido seleccionado: #{oid} | Estado={state} | Confirmado={confirmed or '—'}")
        self.ent_client.delete(0, tk.END); self.ent_client.insert(0, client)
        self.cmb_product.set(product)
        self.cmb_size.set(size)
        try:
            self.spn_qty.delete(0, tk.END); self.spn_qty.insert(0, int(qty))
        except Exception:
            self.spn_qty.delete(0, tk.END); self.spn_qty.insert(0, "1")
        self.cmb_pay.set(pay)

    def _enqueue_mod(self):
        # obtener pedido seleccionado
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Modificar", "Selecciona un pedido primero.")
            return
        oid = int(self.tree.item(sel[0])["values"][0])

        # construir evento
        try:
            qty = int(self.spn_qty.get().strip())
        except ValueError:
            qty = None

        ev = ModEvent(
            urgent=bool(self.var_urgent.get()),
            order_id=oid,
            new_client_name=self.ent_client.get().strip() or None,
            new_product=self.cmb_product.get().strip() or None,
            new_size=self.cmb_size.get().strip() or None,
            new_qty=qty if qty and qty > 0 else None,
            new_payment_method=self.cmb_pay.get().strip() or None
        )
        self.mod_service.enqueue(ev)
        self._push_queue_preview(ev)
        self.status.set(f"Solicitud de modificación encolada (tx={ev.req_id[:8]}…).")

    def _push_queue_preview(self, ev: ModEvent):
        prio = "ALTA" if ev.urgent else "NORMAL"
        qty = ev.new_qty if ev.new_qty else "-"
        self.queue_list.insert("end", f"tx={ev.req_id[:8]} | Pedido #{ev.order_id} | prio={prio} | qty={qty}")

    def _update_metrics(self, m: Dict[str, Any]):
        text = (f"Procesadas={m['processed']} | MOD_OK={m['MOD_OK']} | TIME_EXPIRED={m['TIME_EXPIRED']} | "
                f"SYNC_FAIL={m['SYNC_FAIL']} | MOD_FAIL={m['MOD_FAIL']} | avg_latency={m['avg_latency_ms']:.1f} ms")
        self.lbl_metrics.configure(text="Métricas: " + text)
        # refrescar listado por si cambió algo
        self._refresh_orders()

    def _simulate_concurrent(self):
        """
        Genera 5 modificaciones simultáneas (al azar Urgente/Normal) sobre el pedido seleccionado,
        para evidenciar cola prioritaria y estabilidad.
        """
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Simulación", "Selecciona un pedido para simular.")
            return
        oid = int(self.tree.item(sel[0])["values"][0])

        for i in range(5):
            urgent = random.random() < 0.5
            # variamos qty y (opcional) tamaño/producto
            qty = random.randint(1, 4)
            size = random.choice(["personal","mediana","grande"])
            product = random.choice(["pepperoni","hawaiana"])
            ev = ModEvent(
                urgent=urgent,
                order_id=oid,
                new_qty=qty,
                new_size=size,
                new_product=product
            )
            self.mod_service.enqueue(ev)
            self._push_queue_preview(ev)
        self.status.set("5 modificaciones concurrentes encoladas.")

# =========================
# Main
# =========================
if __name__ == "__main__":
    app = Story6App()
    app.mainloop()
