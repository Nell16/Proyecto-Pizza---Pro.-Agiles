#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher para Sprint Review — HU1..HU6
Lanza cada historia en un proceso independiente.
Requisitos: los archivos huX_*.py deben estar en el mismo directorio que este launcher.
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

TARGETS = [
    ("HU1  Registro de Pedido",       "hu1_RegistroPedido.py"),
    ("HU2  Registro Automático Cocina","hu2_RegistroAutoCocina.py"),
    ("HU3  Recetas Estandarizadas",   "hu3_Recetas.py"),
    ("HU4  Control de Cocción",       "hu4_ControlCoccion.py"),
    ("HU5  Alertas de Sobrecocción",  "hu5_Alertas.py"),
    ("HU6  Modificación de Pedido",   "hu6_ModPedido.py"),
]

class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sprint Review – Launcher HU1..HU6")
        self.geometry("520x420")
        self.minsize(500, 400)
        self.procs = {}  # nombre -> Popen
        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Historias disponibles:", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0,8))

        self.listbox = tk.Listbox(frm, height=10)
        for label, fname in TARGETS:
            exists = os.path.exists(os.path.join(BASE_DIR, fname))
            self.listbox.insert("end", f"{label}  [{fname}] {'✅' if exists else '❌'}")
        self.listbox.pack(fill="both", expand=True)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10,0))
        ttk.Button(btns, text="Abrir seleccionada", command=self.open_selected).pack(side="left")
        ttk.Button(btns, text="Abrir todas", command=self.open_all).pack(side="left", padx=(8,0))
        ttk.Button(btns, text="Cerrar todas", command=self.close_all).pack(side="left", padx=(8,0))

        misc = ttk.Frame(frm)
        misc.pack(fill="x", pady=(10,0))
        ttk.Button(misc, text="Abrir carpeta data/", command=lambda: self._open_folder(DATA_DIR)).pack(side="left")
        ttk.Button(misc, text="Abrir carpeta logs/", command=lambda: self._open_folder(LOGS_DIR)).pack(side="left", padx=(8,0))

        self.status = tk.StringVar(value="Listo.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", side="bottom")

    def open_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("Launcher", "Selecciona una HU en la lista.")
            return
        idx = sel[0]
        self._open_target(idx)

    def open_all(self):
        for idx in range(len(TARGETS)):
            self._open_target(idx)

    def _open_target(self, idx: int):
        label, fname = TARGETS[idx]
        path = os.path.join(BASE_DIR, fname)
        if not os.path.exists(path):
            messagebox.showwarning("Launcher", f"No se encuentra el archivo:\n{fname}")
            return
        if fname in self.procs and self.procs[fname].poll() is None:
            messagebox.showinfo("Launcher", f"{label} ya está abierto.")
            return
        try:
            p = subprocess.Popen([sys.executable, path], cwd=BASE_DIR)
            self.procs[fname] = p
            self.status.set(f"Abrí: {label}")
        except Exception as e:
            messagebox.showerror("Launcher", f"No pude abrir {fname}:\n{e}")

    def close_all(self):
        count = 0
        for fname, p in list(self.procs.items()):
            if p and p.poll() is None:
                try:
                    p.terminate()
                    count += 1
                except Exception:
                    pass
        self.status.set(f"Intenté cerrar {count} procesos.")
        self.after(800, self._refresh_list)

    def _refresh_list(self):
        self.listbox.delete(0, "end")
        for label, fname in TARGETS:
            exists = os.path.exists(os.path.join(BASE_DIR, fname))
            running = (fname in self.procs and self.procs[fname].poll() is None)
            mark = "🟢" if running else ("✅" if exists else "❌")
            self.listbox.insert("end", f"{label}  [{fname}] {mark}")

    def _open_folder(self, path: str):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore
            else:
                cmd = "open" if "darwin" in sys.platform else "xdg-open"
                os.system(f'{cmd} "{path}"')
        except Exception as e:
            messagebox.showwarning("Launcher", f"No pude abrir la carpeta:\n{e}")

    def destroy(self):
        # intentamos cerrar hijos al salir
        self.close_all()
        return super().destroy()

if __name__ == "__main__":
    app = Launcher()
    app.mainloop()
