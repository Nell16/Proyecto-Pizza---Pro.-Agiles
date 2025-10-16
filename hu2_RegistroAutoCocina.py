"""
Historia #2 — Registro automático en cocina
Objetivo: Al confirmar un pedido, debe aparecer en la pantalla de cocina en ≤ 5 s.
Incluye: confirmación, envío asíncrono con reintentos y ACK, logs.

Arquitectura pensada para escalar/estilizar:
- Config centralizada (CONSTANTS)
- Repository (SQLite)
- Services (KitchenSyncService)
- UI desacoplada (Tkinter) con callbacks
"""

import os
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
import time
import random
import threading
from queue import Queue, Empty
from dataclasses import dataclass
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

# =========================
# Config / Constants
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
PEDIDOS_LOG = os.path.join(LOGS_DIR, "pedidos.log")

# Simulación de transporte caja→cocina
MIN_DELAY_S = 0.5
MAX_DELAY_S = 3.2
MAX_TARGET_S = 5.0  # criterio de aceptación

# Reintentos
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.8  # backoff exponencial: 0.8, 1.6, 3.2...

# Placeholder de tema (para estilizar luego)
THEME = {
    "accent": "#1b5e20",
    "warn": "#e65100",
    "ok": "#2e7d32",
    "bg": None,  # cambiar por color de fondo si quieres
}

# =========================
# Logging
# =========================
logger = logging.getLogger("story2")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = RotatingFileHandler(PEDIDOS_LOG, maxBytes=512_000, backupCount=2)
    _fmt = logging.Formatter("[%(asctime)s] %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

# =========================
# Modelos / DTO
# =========================
@dataclass
class OrderRow:
    id: int
    client_name: str
    product: str
    size: str
    qty: int
    payment_method: str
    state: str
    created_at: str
    confirmed_at: str | None

# =========================
# Repositorio (SQLite)
# =========================
class OrderRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    def _ensure_schema(self):
        with self._connect() as con:
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

    def list_orders(self) -> list[OrderRow]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, client_name, product, size, qty, payment_method, state, created_at, confirmed_at
                FROM orders
                ORDER BY id DESC
            """)
            rows = cur.fetchall()
            return [OrderRow(*r) for r in rows]

    def confirm_order(self, order_id: int) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("UPDATE orders SET state='confirmed', confirmed_at=? WHERE id=? AND state!='cancelled'", (now, order_id))
            return cur.rowcount > 0

# =========================
# Servicio de sincronización a cocina
# =========================
class KitchenSyncService:
    """
    - Envía pedidos confirmados al panel de cocina con delay random (0.5–3.2 s).
    - Si “falla” (simulación), reintenta con backoff.
    - Llama a callback_ui(linea) en el hilo UI.
    """
    def __init__(self, ui_callback_append):
        self.ui_callback_append = ui_callback_append
        self.queue: "Queue[tuple[int, float, dict]]" = Queue()
        self._stop = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def stop(self):
        self._stop.set()
        self.queue.put((-1, 0, {}))

    def send_confirmed(self, order: OrderRow):
        # payload extra con datos convenientemente formateados
        payload = {
            "id": order.id,
            "name": order.client_name,
            "label": f"{order.product} {order.size} x{order.qty}",
            "created_at": order.created_at
        }
        created_ts = datetime.strptime(order.created_at, "%Y-%m-%d %H:%M:%S").timestamp()
        self.queue.put((order.id, created_ts, payload))

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                oid, created_ts, payload = self.queue.get(timeout=0.5)
            except Empty:
                continue
            if oid == -1:
                break

            # Simular transporte
            delay = random.uniform(MIN_DELAY_S, MAX_DELAY_S)
            time.sleep(delay)

            # Simulación de fallo raro (p.ej. 8% prob)
            fail = random.random() < 0.08
            if fail:
                self._retry_send(created_ts, payload)
            else:
                self._deliver(created_ts, payload)

            self.queue.task_done()

    def _retry_send(self, created_ts: float, payload: dict):
        backoff = INITIAL_BACKOFF_S
        retries = 0
        while retries < MAX_RETRIES:
            time.sleep(backoff)
            # Simular que reintento tiene alta probabilidad de éxito
            if random.random() < 0.85:
                self._deliver(created_ts, payload, retried=True, retries=retries+1)
                return
            retries += 1
            backoff *= 2  # exponencial

        # Si agotó reintentos, igual “entrega” con marca de tardío (para evidenciar)
        self._deliver(created_ts, payload, retried=True, retries=retries, forced=True)

    def _deliver(self, created_ts: float, payload: dict, retried: bool=False, retries: int=0, forced: bool=False):
        received = time.time()
        delta = received - created_ts
        status = "OK"
        if retried and not forced:
            status = f"OK (reintento x{retries})"
        if forced:
            status = f"TARDÍO (x{retries} reintentos)"

        line = f"Pedido #{payload['id']} | {payload['label']} | Llegó en {delta:.2f}s | Estado: {status}"
        # Log
        logger.info(line)
        # UI
        self.ui_callback_append(line)
        # Marcar si excede el objetivo
        if delta > MAX_TARGET_S:
            warn = f"⚠ SLA excedido (> {MAX_TARGET_S:.0f}s) para Pedido #{payload['id']}"
            logger.info(warn)
            self.ui_callback_append(warn)

# =========================
# UI (Tkinter)
# =========================
class Story2App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #2 — Registro automático en cocina (≤ 5 s)")
        self.geometry("1080x640")
        self.minsize(960, 580)

        self.repo = OrderRepository(DB_PATH)
        self.sync_service = KitchenSyncService(self._kitchen_append_ui)

        self._build_ui()
        self._refresh_orders()

    def destroy(self):
        # Apagar worker con elegancia
        try:
            self.sync_service.stop()
        finally:
            return super().destroy()

    def _build_ui(self):
        nb = ttk.Notebook(self)
        self.tab_confirm = ttk.Frame(nb)
        self.tab_kitchen = ttk.Frame(nb)
        nb.add(self.tab_confirm, text="Confirmar y enviar a cocina")
        nb.add(self.tab_kitchen, text="Pantalla de Cocina")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_confirm(self.tab_confirm)
        self._build_tab_kitchen(self.tab_kitchen)

        # status bar
        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w", foreground=THEME["accent"])
        status_bar.pack(fill="x", side="bottom")

    # -------- Tab Confirm --------
    def _build_tab_confirm(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        # Listado de pedidos
        cols = ("id", "cliente", "producto", "tamano", "cant", "pago", "estado", "creado", "confirmado")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=18)
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
        widths = {"id":60, "cliente":160, "producto":110, "tamano":90, "cant":60, "pago":120, "estado":100, "creado":160, "confirmado":160}
        for c in cols:
            self.tree.column(c, width=widths[c], anchor="center" if c not in ("cliente",) else "w")
        self.tree.pack(fill="both", expand=True)

        # Acciones
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Actualizar", command=self._refresh_orders).pack(side="left")
        ttk.Button(btns, text="Confirmar seleccionado", command=self._confirm_selected).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Abrir logs", command=self._open_logs).pack(side="left", padx=(8, 0))

        hint = ttk.Label(frm, text=f"Criterio: llegada a cocina en ≤ {MAX_TARGET_S:.0f} s. Evidencias en {PEDIDOS_LOG}.", foreground="#616161")
        hint.pack(anchor="w", pady=(8, 0))

    def _build_tab_kitchen(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        header = ttk.Label(frm, text="Panel de Cocina (sincronización y ACK)", font=("TkDefaultFont", 12, "bold"))
        header.pack(anchor="w", pady=(0, 8))

        self.kitchen_list = tk.Listbox(frm, height=20)
        self.kitchen_list.pack(fill="both", expand=True)

        hint = ttk.Label(frm, text="Llega con latencia simulada (0.5–3.2 s). Con reintentos ocasionales.", foreground="#616161")
        hint.pack(anchor="w", pady=(8, 0))

    # -------- Actions --------
    def _refresh_orders(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for o in self.repo.list_orders():
            self.tree.insert("", "end", values=(o.id, o.client_name, o.product, o.size, o.qty, o.payment_method, o.state, o.created_at, o.confirmed_at))

    def _confirm_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Selecciona un pedido en la tabla.")
            return
        item_id = sel[0]
        row = self.tree.item(item_id)["values"]
        order_id = int(row[0])

        # Confirmar en BD
        ok = self.repo.confirm_order(order_id)
        if not ok:
            messagebox.showwarning("Atención", "No fue posible confirmar (¿ya estaba cancelado?).")
            return

        # Refrescar listado
        self._refresh_orders()

        # Cargar orden confirmada para envío a cocina
        # (en listado ordenado DESC, podemos volver a obtener la fila; o crear un objeto row)
        confirmed = None
        for o in self.repo.list_orders():
            if o.id == order_id:
                confirmed = o
                break
        if confirmed is None:
            messagebox.showwarning("Atención", "No se encontró el pedido confirmado.")
            return

        # Enviar a cocina (asíncrono)
        self.sync_service.send_confirmed(confirmed)
        self.status.set(f"Pedido #{order_id} confirmado. Enviando a cocina…")

    def _open_logs(self):
        try:
            if os.name == "nt":
                os.startfile(LOGS_DIR)  # type: ignore[attr-defined]
            else:
                cmd = "open" if "darwin" in os.sys.platform else "xdg-open"
                os.system(f'{cmd} "{LOGS_DIR}"')
        except Exception as e:
            messagebox.showwarning("Abrir carpeta", f"No se pudo abrir la carpeta:\n{e}")

    # Thread-safe append
    def _kitchen_append_ui(self, line: str):
        self.after(0, lambda: self.kitchen_list.insert("end", line))

# =========================
# Main
# =========================
if __name__ == "__main__":
    app = Story2App()
    if THEME["bg"]:
        app.configure(bg=THEME["bg"])  # estiliza luego sin tocar lógica
    app.mainloop()
