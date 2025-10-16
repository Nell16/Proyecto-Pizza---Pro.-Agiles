"""
Historia #3 — Recetas estandarizadas
Cubre:
- Recetas almacenadas en BD con medidas exactas (ingredientes/unidades).
- Cualquier cocinero ve la misma receta (única versión vigente por pizza).
- Versionado con historial completo y filtros (tipo y fecha).
- Validaciones: impide guardar valores vacíos o fuera de rango.
- Indicador visual de receta vigente.
- Export a JSON/CSV (auditoría).
- Preparado para edición simultánea (transacciones / UPDATE + INSERT atómicos).

Tecnologías: Tkinter (GUI), sqlite3 (BD), logging (evidencias)
"""

import os
import json
import csv
import re
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# =========================
# Rutas / Config
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
LOG_PATH = os.path.join(LOGS_DIR, "coccion.log")  # mantenemos compatibilidad con Historia 4/5

THEME = {
    "accent": "#1b5e20",
    "warn": "#e65100",
    "ok": "#2e7d32",
}

VALID_PIZZAS = ("pepperoni", "hawaiana")  # amplía a futuro

# =========================
# Logging
# =========================
logger = logging.getLogger("story3_recetas")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2)
    _fmt = logging.Formatter("[%(asctime)s] %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

# =========================
# Utils
# =========================
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_date(s: str) -> Optional[str]:
    s = s.strip()
    if not s:
        return None
    # soporta YYYY-MM-DD o YYYY/MM/DD
    s = s.replace("/", "-")
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None

def validate_ingredients(ingredients: Dict[str, str]) -> Optional[str]:
    """
    Valida que:
    - claves no vacías
    - valores tipo '150g' o '90 g' o '0.5kg' (g/kg permitidos)
    - >0
    """
    unit_pat = re.compile(r"^\s*(\d+(\.\d+)?)\s*(g|kg)\s*$", re.IGNORECASE)
    for k, v in ingredients.items():
        if not k.strip():
            return "Hay un ingrediente sin nombre."
        if not v.strip():
            return f"'{k}': cantidad vacía."
        m = unit_pat.match(v)
        if not m:
            return f"'{k}': usa cantidades tipo '150g' o '0.5kg'."
        val = float(m.group(1))
        unit = m.group(3).lower()
        grams = val * (1000 if unit == "kg" else 1)
        if grams <= 0:
            return f"'{k}': la cantidad debe ser > 0."
        if grams > 5000:
            return f"'{k}': la cantidad es demasiado alta (> 5000g)."
    return None

def validate_tolerance(tol_percent: str) -> Optional[str]:
    """
    Acepta: '±2%' o '2%' o '0-3%'
    Se normaliza a formato '±X%'
    """
    s = tol_percent.strip().replace(" ", "")
    if not s:
        return "La tolerancia no puede estar vacía."
    if s.startswith("±"):
        s = s[1:]
    if s.endswith("%"):
        s = s[:-1]
    # rango "a-b" o single
    if "-" in s:
        a, b = s.split("-", 1)
        try:
            a = float(a); b = float(b)
        except ValueError:
            return "Tolerancia inválida (usa números)."
        if a < 0 or b < 0 or a > b or b > 10:
            return "Tolerancia fuera de rango (0–10%)."
        return None
    else:
        try:
            x = float(s)
        except ValueError:
            return "Tolerancia inválida (usa números)."
        if x < 0 or x > 10:
            return "Tolerancia fuera de rango (0–10%)."
        return None

def normalize_tolerance(tol_percent: str) -> str:
    s = tol_percent.strip().replace(" ", "")
    if s.startswith("±"):
        return s.upper() if s.endswith("%") else s + "%"
    if "-" in s:
        if not s.endswith("%"):
            s += "%"
        return s
    # single
    if s.endswith("%"):
        s = s[:-1]
    try:
        x = float(s)
        return f"±{x}%"
    except:
        return tol_percent  # fallback

# =========================
# Modelo / Repositorio SQLite
# =========================
@dataclass
class RecipeVersionRow:
    id: int
    pizza_type: str
    version: int
    active: int           # 0/1
    ingredients_json: str
    tolerance_json: str   # {"%": "±2%"} u otros campos si amplías
    note: str
    created_at: str

class RecipeRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    def _ensure_schema(self):
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS recipe_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pizza_type TEXT NOT NULL,
                version INTEGER NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                ingredients_json TEXT NOT NULL,
                tolerance_json TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """)
            # índices para filtros
            cur.execute("CREATE INDEX IF NOT EXISTS idx_recipe_versions_type ON recipe_versions(pizza_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_recipe_versions_created ON recipe_versions(created_at)")
            con.commit()
        # seed inicial si vacío
        if not self.list_types():
            self.seed_initial()

    def seed_initial(self):
        seed = {
            "pepperoni": {"masa": "260g", "salsa": "90g", "queso": "150g", "pepperoni": "60g"},
            "hawaiana": {"masa": "260g", "salsa": "90g", "queso": "150g", "jamon": "50g", "piña": "70g"},
        }
        for ptype, ingr in seed.items():
            self.create_new_version(
                pizza_type=ptype,
                ingredients=ingr,
                tolerance={"%": "±2%"},
                note="Receta base inicial",
                activate=True
            )

    def list_types(self) -> List[str]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT DISTINCT pizza_type FROM recipe_versions ORDER BY pizza_type")
            return [r[0] for r in cur.fetchall()]

    def list_versions(self, pizza_type: str,
                      date_from: Optional[str] = None,
                      date_to: Optional[str] = None) -> List[RecipeVersionRow]:
        q = """
          SELECT id, pizza_type, version, active, ingredients_json, tolerance_json, note, created_at
          FROM recipe_versions
          WHERE pizza_type=?
        """
        params: List[Any] = [pizza_type]
        if date_from:
            q += " AND date(created_at) >= date(?)"
            params.append(date_from)
        if date_to:
            q += " AND date(created_at) <= date(?)"
            params.append(date_to)
        q += " ORDER BY version DESC"
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(q, tuple(params))
            rows = cur.fetchall()
            return [RecipeVersionRow(*r) for r in rows]

    def get_active(self, pizza_type: str) -> Optional[RecipeVersionRow]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
            SELECT id, pizza_type, version, active, ingredients_json, tolerance_json, note, created_at
            FROM recipe_versions
            WHERE pizza_type=? AND active=1
            """, (pizza_type,))
            r = cur.fetchone()
            return RecipeVersionRow(*r) if r else None

    def create_new_version(self, pizza_type: str, ingredients: Dict[str, str],
                           tolerance: Dict[str, str], note: str, activate: bool = True) -> int:
        """
        Transacción: desactiva todas + inserta nueva con version = max+1 y activa=true.
        """
        created_at = now_str()
        with self._connect() as con:
            cur = con.cursor()
            # lock implícito por transacción (isolation_level=None usa autocommit,
            # aseguramos atomicidad con BEGIN EXCLUSIVE)
            cur.execute("BEGIN EXCLUSIVE")
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM recipe_versions WHERE pizza_type=?", (pizza_type,))
            maxver = cur.fetchone()[0] or 0
            nextver = int(maxver) + 1
            if activate:
                cur.execute("UPDATE recipe_versions SET active=0 WHERE pizza_type=?", (pizza_type,))
            cur.execute("""
                INSERT INTO recipe_versions(pizza_type, version, active, ingredients_json, tolerance_json, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                pizza_type,
                nextver,
                1 if activate else 0,
                json.dumps(ingredients, ensure_ascii=False),
                json.dumps(tolerance, ensure_ascii=False),
                note,
                created_at
            ))
            rid = cur.lastrowid
            con.commit()

        logger.info(f"Receta actualizada | {pizza_type} v{nextver} | {json.dumps(ingredients, ensure_ascii=False)}")
        return rid

# =========================
# GUI
# =========================
class Story3App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #3 — Recetas estandarizadas (Versionado e Historial)")
        self.geometry("1120x720")
        self.minsize(1024, 640)
        self.repo = RecipeRepository(DB_PATH)

        self._build_ui()
        self._load_types()

    # ---------- UI Build ----------
    def _build_ui(self):
        nb = ttk.Notebook(self)
        self.tab_hist = ttk.Frame(nb)
        self.tab_new = ttk.Frame(nb)
        nb.add(self.tab_hist, text="Historial / Vigente / Filtros")
        nb.add(self.tab_new, text="Nueva versión")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_hist(self.tab_hist)
        self._build_tab_new(self.tab_new)

        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w", foreground=THEME["accent"])
        status_bar.pack(fill="x", side="bottom")

    def _build_tab_hist(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        # Filtros
        f_filters = ttk.LabelFrame(frm, text="Filtros", padding=10)
        f_filters.pack(fill="x")

        ttk.Label(f_filters, text="Tipo de pizza").grid(row=0, column=0, sticky="w")
        self.cmb_type = ttk.Combobox(f_filters, state="readonly", width=24)
        self.cmb_type.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.cmb_type.bind("<<ComboboxSelected>>", lambda e: self._refresh_versions())

        ttk.Label(f_filters, text="Desde (YYYY-MM-DD)").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.ent_from = ttk.Entry(f_filters, width=16)
        self.ent_from.grid(row=1, column=1, sticky="w", padx=(12, 0))

        ttk.Label(f_filters, text="Hasta (YYYY-MM-DD)").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.ent_to = ttk.Entry(f_filters, width=16)
        self.ent_to.grid(row=1, column=2, sticky="w", padx=(12, 0))

        ttk.Button(f_filters, text="Aplicar filtros", command=self._refresh_versions).grid(row=1, column=3, padx=(12, 0))
        ttk.Button(f_filters, text="Limpiar", command=self._clear_filters).grid(row=1, column=4, padx=(6, 0))
        ttk.Button(f_filters, text="Exportar JSON", command=self._export_json).grid(row=1, column=5, padx=(18, 0))
        ttk.Button(f_filters, text="Exportar CSV", command=self._export_csv).grid(row=1, column=6, padx=(6, 0))

        # Panel izq: tabla historial
        f_hist = ttk.LabelFrame(frm, text="Historial de versiones", padding=10)
        f_hist.pack(side="left", fill="both", expand=True, pady=(10, 0))

        cols = ("id", "version", "vigente", "creada", "nota")
        self.tree = ttk.Treeview(f_hist, columns=cols, show="headings", height=22)
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
        widths = {"id":70, "version":80, "vigente":80, "creada":170, "nota":280}
        for c in cols:
            self.tree.column(c, width=widths[c], anchor="center" if c != "nota" else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())

        # Panel der: detalles de la versión
        f_det = ttk.LabelFrame(frm, text="Detalles de la versión seleccionada", padding=10)
        f_det.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(10, 0))

        self.txt_details = tk.Text(f_det, height=24)
        self.txt_details.pack(fill="both", expand=True)

        self.lbl_active = ttk.Label(f_det, text="Vigente: —", foreground=THEME["ok"])
        self.lbl_active.pack(anchor="w", pady=(8, 0))

    def _build_tab_new(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)

        left = ttk.LabelFrame(frm, text="Nueva versión", padding=10)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="Tipo de pizza").grid(row=0, column=0, sticky="w")
        self.cmb_type_new = ttk.Combobox(left, values=list(VALID_PIZZAS), state="readonly", width=24)
        self.cmb_type_new.set(VALID_PIZZAS[0])
        self.cmb_type_new.grid(row=1, column=0, sticky="w", pady=(0, 10))

        ttk.Label(left, text="Ingredientes (uno por línea: nombre=cantidad, ej. queso=150g)").grid(row=2, column=0, sticky="w")
        self.txt_ing = tk.Text(left, width=38, height=12)
        self.txt_ing.grid(row=3, column=0, pady=(0, 8))

        ttk.Label(left, text="Tolerancia (%) [ej. ±2%  ó  0-3%]").grid(row=4, column=0, sticky="w")
        self.ent_tol = ttk.Entry(left, width=26)
        self.ent_tol.insert(0, "±2%")
        self.ent_tol.grid(row=5, column=0, pady=(0, 10))

        ttk.Label(left, text="Nota (opcional)").grid(row=6, column=0, sticky="w")
        self.ent_note = ttk.Entry(left, width=36)
        self.ent_note.grid(row=7, column=0, pady=(0, 10))

        self.var_activate = tk.IntVar(value=1)
        ttk.Checkbutton(left, text="Activar esta versión como vigente", variable=self.var_activate).grid(row=8, column=0, sticky="w")

        ttk.Button(left, text="Crear versión", command=self._create_version).grid(row=9, column=0, sticky="ew", pady=(10, 0))

        right = ttk.LabelFrame(frm, text="Ayuda y criterios", padding=10)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        help_text = (
            "Criterios:\n"
            "• Medidas exactas en BD (ingredientes, unidades).\n"
            "• Única receta vigente por pizza (se desactivan las anteriores).\n"
            "• Historial completo consultable y exportable.\n"
            "• Validaciones: sin vacíos, cantidades >0, tolerancia en rango 0–10%.\n\n"
            "Tips:\n"
            "• Usa '150g' o '0.5kg' en cantidades.\n"
            "• Tolerancia: '±2%' o '0-3%'.\n"
            "• Filtra por fecha en la pestaña Historial."
        )
        ttk.Label(right, text=help_text, justify="left").pack(anchor="w")

    # ---------- Data Loading / Actions ----------
    def _load_types(self):
        types = self.repo.list_types()
        if not types:
            types = list(VALID_PIZZAS)
        self.cmb_type["values"] = types
        self.cmb_type.set(types[0])
        self._refresh_versions()

    def _refresh_versions(self):
        ptype = self.cmb_type.get().strip()
        dfrom = parse_date(self.ent_from.get())
        dto = parse_date(self.ent_to.get())
        if self.ent_from.get().strip() and not dfrom:
            messagebox.showwarning("Filtro", "Fecha 'Desde' inválida (usa YYYY-MM-DD).")
            return
        if self.ent_to.get().strip() and not dto:
            messagebox.showwarning("Filtro", "Fecha 'Hasta' inválida (usa YYYY-MM-DD).")
            return

        for r in self.tree.get_children():
            self.tree.delete(r)

        rows = self.repo.list_versions(ptype, dfrom, dto)
        for rv in rows:
            self.tree.insert("", "end", iid=str(rv.id),
                             values=(rv.id, rv.version, "Sí" if rv.active else "No", rv.created_at, rv.note or ""))

        act = self.repo.get_active(ptype)
        self.lbl_active.configure(text=f"Vigente: v{act.version}" if act else "Vigente: —")
        self.txt_details.delete("1.0", tk.END)

    def _clear_filters(self):
        self.ent_from.delete(0, tk.END)
        self.ent_to.delete(0, tk.END)
        self._refresh_versions()

    def _show_details(self):
        sel = self.tree.selection()
        if not sel:
            return
        rid = int(sel[0])
        ptype = self.cmb_type.get().strip()
        rows = self.repo.list_versions(ptype)
        pick = next((x for x in rows if x.id == rid), None)
        if not pick:
            return
        self.txt_details.delete("1.0", tk.END)
        ing = json.loads(pick.ingredients_json)
        tol = json.loads(pick.tolerance_json)
        lines = [f"Tipo: {pick.pizza_type}",
                 f"Versión: {pick.version}",
                 f"Vigente: {'Sí' if pick.active else 'No'}",
                 f"Fecha: {pick.created_at}",
                 f"Nota: {pick.note or '-'}",
                 "",
                 "Ingredientes:"]
        for k, v in ing.items():
            lines.append(f"  - {k}: {v}")
        lines.append("")
        lines.append(f"Tolerancia: {tol.get('%','—')}")
        self.txt_details.insert("1.0", "\n".join(lines))

    def _parse_ingredients_text(self) -> Dict[str, str]:
        raw = self.txt_ing.get("1.0", tk.END).strip().splitlines()
        pairs: Dict[str, str] = {}
        for line in raw:
            if not line.strip():
                continue
            if "=" not in line:
                raise ValueError(f"Línea inválida: '{line}'. Usa nombre=cantidad (ej. queso=150g).")
            k, v = line.split("=", 1)
            pairs[k.strip()] = v.strip()
        if not pairs:
            raise ValueError("Debes ingresar al menos un ingrediente.")
        return pairs

    def _create_version(self):
        ptype = self.cmb_type_new.get().strip()
        try:
            ingredients = self._parse_ingredients_text()
        except ValueError as e:
            messagebox.showerror("Ingredientes", str(e))
            return

        err_ing = validate_ingredients(ingredients)
        if err_ing:
            messagebox.showerror("Validación", err_ing)
            return

        tol_raw = self.ent_tol.get().strip()
        err_tol = validate_tolerance(tol_raw)
        if err_tol:
            messagebox.showerror("Tolerancia", err_tol)
            return
        tol = {"%": normalize_tolerance(tol_raw)}

        note = self.ent_note.get().strip()
        activate = bool(self.var_activate.get())

        try:
            rid = self.repo.create_new_version(ptype, ingredients, tol, note, activate=activate)
        except sqlite3.Error as e:
            messagebox.showerror("BD", f"Error guardando versión: {e}")
            return

        messagebox.showinfo("Éxito", f"Nueva versión creada (ID {rid}). {'Ahora es la vigente.' if activate else ''}")
        # refresh ambas pestañas
        self._load_types()
        self.status.set(f"Versión creada para {ptype}.")
        # limpiar inputs
        self.txt_ing.delete("1.0", tk.END)
        self.ent_note.delete(0, tk.END)

    # ---------- Export ----------
    def _export_json(self):
        ptype = self.cmb_type.get().strip()
        dfrom = parse_date(self.ent_from.get())
        dto = parse_date(self.ent_to.get())
        rows = self.repo.list_versions(ptype, dfrom, dto)
        data = []
        for r in rows:
            data.append({
                "id": r.id,
                "pizza_type": r.pizza_type,
                "version": r.version,
                "active": bool(r.active),
                "ingredients": json.loads(r.ingredients_json),
                "tolerance": json.loads(r.tolerance_json),
                "note": r.note,
                "created_at": r.created_at
            })
        if not data:
            messagebox.showinfo("Exportar", "No hay datos para exportar.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")],
                                            initialfile=f"recetas_{ptype}.json")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("Exportar", f"Exportado JSON: {os.path.basename(path)}")

    def _export_csv(self):
        ptype = self.cmb_type.get().strip()
        dfrom = parse_date(self.ent_from.get())
        dto = parse_date(self.ent_to.get())
        rows = self.repo.list_versions(ptype, dfrom, dto)
        if not rows:
            messagebox.showinfo("Exportar", "No hay datos para exportar.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile=f"recetas_{ptype}.csv")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "pizza_type", "version", "active", "created_at", "note", "ingredients_json", "tolerance_json"])
            for r in rows:
                writer.writerow([r.id, r.pizza_type, r.version, r.active, r.created_at, r.note or "",
                                 r.ingredients_json, r.tolerance_json])
        messagebox.showinfo("Exportar", f"Exportado CSV: {os.path.basename(path)}")

# =========================
# Main
# =========================
if __name__ == "__main__":
    app = Story3App()
    app.mainloop()
