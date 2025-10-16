#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Historia #1 — Registro digital de pedidos
Objetivo: El cliente registra su pedido desde interfaz, con validaciones, persistencia en BD
(SQLite), mensajes claros y tiempo de flujo muy por debajo de 2 minutos.

Cumple criterios:
- Inicio desde interfaz (simulación de app/web con GUI).
- Guardado en BD sin errores (SQLite).
- Flujo rápido: creación suele tomar < 1–3 s (muy por debajo de 120 s).

Tecnologías: Tkinter (GUI), sqlite3 (BD), logging (evidencias)
"""

import os
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from datetime import datetime
import time

# --------------------------
# Rutas (BD y logs)
# --------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
LOG_PATH = os.path.join(LOGS_DIR, "pedidos.log")

# --------------------------
# Logging
# --------------------------
logger = logging.getLogger("story1")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2)
    _fmt = logging.Formatter("[%(asctime)s] %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

# --------------------------
# Modelo de datos
# --------------------------
@dataclass
class OrderDTO:
    client_name: str
    product: str       # "pepperoni" | "hawaiana" (amplía luego)
    size: str          # "personal" | "mediana" | "grande"
    qty: int
    payment_method: str  # "efectivo" | "tarjeta" | "transferencia"

# --------------------------
# Capa de persistencia (SQLite)
# --------------------------
class OrderRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)  # autocommit

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

    def create_order(self, dto: OrderDTO) -> int:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO orders(client_name, product, size, qty, payment_method, state, created_at)
                VALUES (?, ?, ?, ?, ?, 'draft', ?)
            """, (dto.client_name, dto.product, dto.size, dto.qty, dto.payment_method, created_at))
            order_id = cur.lastrowid
            logger.info(f"CREATED | order_id={order_id} | {dto.client_name} | {dto.product}/{dto.size} x{dto.qty} | {dto.payment_method}")
            return order_id

    def list_orders(self):
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, client_name, product, size, qty, payment_method, state, created_at, confirmed_at
                FROM orders
                ORDER BY id DESC
            """)
            return cur.fetchall()

# --------------------------
# Validaciones de negocio
# --------------------------
VALID_PRODUCTS = ("pepperoni", "hawaiana")
VALID_SIZES = ("personal", "mediana", "grande")
VALID_PAYMENTS = ("efectivo", "tarjeta", "transferencia")

def validate_dto(dto: OrderDTO) -> str | None:
    if not dto.client_name.strip():
        return "El nombre del cliente es obligatorio."
    if dto.product not in VALID_PRODUCTS:
        return f"Producto inválido. Usa: {', '.join(VALID_PRODUCTS)}."
    if dto.size not in VALID_SIZES:
        return f"Tamaño inválido. Usa: {', '.join(VALID_SIZES)}."
    if not isinstance(dto.qty, int) or dto.qty <= 0:
        return "La cantidad debe ser un entero mayor que 0."
    if dto.payment_method not in VALID_PAYMENTS:
        return f"Método de pago inválido. Usa: {', '.join(VALID_PAYMENTS)}."
    return None

# --------------------------
# GUI (Tkinter)
# --------------------------
class Story1App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #1 — Registro digital de pedidos (Cliente)")
        self.geometry("980x620")
        self.minsize(900, 560)

        self.repo = OrderRepository(DB_PATH)
        self._build_ui()
        self._refresh_table()

    def _build_ui(self):
        # Tabs para simular flujo (registro + consulta rápida)
        nb = ttk.Notebook(self)
        self.tab_registro = ttk.Frame(nb)
        self.tab_listado = ttk.Frame(nb)
        nb.add(self.tab_registro, text="Registro de pedido (Cliente)")
        nb.add(self.tab_listado, text="Listado / Evidencias")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_registro(self.tab_registro)
        self._build_tab_listado(self.tab_listado)

        # Barra de estado
        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w")
        status_bar.pack(fill="x", side="bottom")

    # -------- Tab 1: Registro --------
    def _build_tab_registro(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        left = ttk.LabelFrame(frm, text="Datos del pedido", padding=12)
        left.pack(side="left", fill="y")

        # Nombre
        ttk.Label(left, text="Nombre del cliente").grid(row=0, column=0, sticky="w")
        self.ent_name = ttk.Entry(left, width=28)
        self.ent_name.grid(row=1, column=0, pady=(0, 10))

        # Producto
        ttk.Label(left, text="Producto").grid(row=2, column=0, sticky="w")
        self.cmb_product = ttk.Combobox(left, values=list(VALID_PRODUCTS), state="readonly", width=25)
        self.cmb_product.set(VALID_PRODUCTS[0])
        self.cmb_product.grid(row=3, column=0, pady=(0, 10))

        # Tamaño
        ttk.Label(left, text="Tamaño").grid(row=4, column=0, sticky="w")
        self.cmb_size = ttk.Combobox(left, values=list(VALID_SIZES), state="readonly", width=25)
        self.cmb_size.set(VALID_SIZES[1])
        self.cmb_size.grid(row=5, column=0, pady=(0, 10))

        # Cantidad
        ttk.Label(left, text="Cantidad").grid(row=6, column=0, sticky="w")
        self.spn_qty = tk.Spinbox(left, from_=1, to=50, width=8)
        self.spn_qty.grid(row=7, column=0, sticky="w", pady=(0, 10))

        # Pago
        ttk.Label(left, text="Método de pago").grid(row=8, column=0, sticky="w")
        self.cmb_pay = ttk.Combobox(left, values=list(VALID_PAYMENTS), state="readonly", width=25)
        self.cmb_pay.set(VALID_PAYMENTS[1])
        self.cmb_pay.grid(row=9, column=0, pady=(0, 10))

        # Botón Crear
        self.btn_create = ttk.Button(left, text="Crear pedido", command=self._on_create_clicked)
        self.btn_create.grid(row=10, column=0, sticky="ew", pady=(6, 4))

        # Métricas de tiempo (para Sprint Review)
        self.lbl_time = ttk.Label(left, text="Tiempo de creación: —")
        self.lbl_time.grid(row=11, column=0, sticky="w", pady=(6, 0))

        # Panel a la derecha: ayuda y criterios
        right = ttk.LabelFrame(frm, text="Criterios de aceptación y ayuda", padding=12)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        help_txt = (
            "Criterios:\n"
            "• El cliente puede iniciar pedido desde interfaz.\n"
            "• El pedido se guarda en BD sin errores.\n"
            "• Flujo completo ≤ 2 minutos (esta demo tarda < 3 s).\n\n"
            "Tips Review:\n"
            "• Demuestra validaciones (campos obligatorios).\n"
            "• Muestra el log y el registro recién creado en la pestaña de Listado."
        )
        ttk.Label(right, text=help_txt, justify="left").pack(anchor="w")

    # -------- Tab 2: Listado / Evidencias --------
    def _build_tab_listado(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(fill="x")
        ttk.Button(top, text="Actualizar listado", command=self._refresh_table).pack(side="left")
        ttk.Button(top, text="Abrir carpeta de logs", command=self._open_logs_folder).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Abrir carpeta de datos", command=self._open_data_folder).pack(side="left", padx=(8, 0))

        cols = ("id", "cliente", "producto", "tamano", "cant", "pago", "estado", "creado", "confirmado")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=18)
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
        self.tree.column("id", width=60, anchor="center")
        self.tree.column("cliente", width=180)
        self.tree.column("producto", width=110, anchor="center")
        self.tree.column("tamano", width=90, anchor="center")
        self.tree.column("cant", width=60, anchor="center")
        self.tree.column("pago", width=120, anchor="center")
        self.tree.column("estado", width=90, anchor="center")
        self.tree.column("creado", width=160, anchor="center")
        self.tree.column("confirmado", width=160, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=(8, 0))

        hint = ttk.Label(frm, text=f"Evidencias en {LOG_PATH}", foreground="#616161")
        hint.pack(anchor="w", pady=(8, 0))

    # -------- Handlers --------
    def _on_create_clicked(self):
        # Medición de tiempo para el “flujo”
        t0 = time.time()

        name = self.ent_name.get().strip()
        product = self.cmb_product.get().strip()
        size = self.cmb_size.get().strip()
        try:
            qty = int(self.spn_qty.get().strip())
        except ValueError:
            qty = 0
        pay = self.cmb_pay.get().strip()

        dto = OrderDTO(
            client_name=name,
            product=product,
            size=size,
            qty=qty,
            payment_method=pay
        )

        err = validate_dto(dto)
        if err:
            messagebox.showerror("Validación", err)
            return

        try:
            order_id = self.repo.create_order(dto)
        except sqlite3.IntegrityError as e:
            messagebox.showerror("BD", f"Error de integridad: {e}")
            return
        except sqlite3.Error as e:
            messagebox.showerror("BD", f"Error de base de datos: {e}")
            return

        elapsed = time.time() - t0
        self.lbl_time.configure(text=f"Tiempo de creación: {elapsed:.2f} s")
        self.status.set(f"Pedido #{order_id} creado correctamente.")
        messagebox.showinfo("Éxito", f"Pedido #{order_id} creado en {elapsed:.2f} s.")
        self._refresh_table()
        # Opcional: limpiar campos
        # self.ent_name.delete(0, tk.END)

    def _refresh_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for (id_, client, product, size, qty, pay, state, created, confirmed) in self.repo.list_orders():
            self.tree.insert("", "end", values=(id_, client, product, size, qty, pay, state, created, confirmed))

    def _open_logs_folder(self):
        self._open_folder(LOGS_DIR)

    def _open_data_folder(self):
        self._open_folder(DATA_DIR)

    def _open_folder(self, path: str):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif os.name == "posix":
                # macOS: 'open', Linux: 'xdg-open'
                cmd = "open" if "darwin" in os.sys.platform else "xdg-open"
                os.system(f'{cmd} "{path}"')
        except Exception as e:
            messagebox.showwarning("Abrir carpeta", f"No se pudo abrir la carpeta:\n{e}")

# --------------------------
# Main
# --------------------------
if __name__ == "__main__":
    app = Story1App()
    app.mainloop()
