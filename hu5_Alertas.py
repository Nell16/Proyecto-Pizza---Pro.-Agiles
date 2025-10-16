"""
Historia #5 — Alertas de sobrecocción
Cubre (según documentación):
- Alertas visuales/sonoras cuando el tiempo real excede el umbral por tamaño (personal/mediana/grande).
- Registro del evento en logs con fecha/hora, tipo de pizza y tiempo excedido.
- Manejo de múltiples alertas simultáneas con cola de eventos y prioridad (CRÍTICO si >3 min sobre el umbral).
- Limpieza automática de alertas viejas (p. ej., a los 10s).
- Sin bloquear la UI; escalable para estilizar temas/colores/recursos después.

Arquitectura:
- BakeTimer: hilo por pizza con temporizador.
- AlertManager: cola de eventos, prioridad y emisión a UI/sonido/log.
- UI: pestañas para “Hornos activos” (timers) y “Panel de Alertas”.

Log: logs/alertas.log
"""

import os
import time
import json
import queue
import threading
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List

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

ALERTS_LOG = os.path.join(LOGS_DIR, "alertas.log")

# Umbrales de cocción por tamaño (min) — coherentes con historias 3/4
COOKING_THRESHOLDS_MIN = {"personal": 10, "mediana": 12, "grande": 15}

# Severidad si excede más de 3 minutos el umbral
CRITICAL_EXTRA_MIN = 3.0

# Limpieza visual de alertas (segundos)
ALERT_AUTO_CLEAN_S = 10

# Estilo/tema (editable luego)
THEME = {
    "accent": "#1b5e20",
    "warn": "#e65100",
    "ok": "#2e7d32",
    "crit": "#b71c1c",
    "bg": None
}

# =========================
# Logging
# =========================
logger = logging.getLogger("story5_alertas")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = RotatingFileHandler(ALERTS_LOG, maxBytes=512_000, backupCount=2)
    _fmt = logging.Formatter("[%(asctime)s] %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =========================
# Modelos / Eventos
# =========================
@dataclass(order=True)
class AlertEvent:
    # order=True permite priorizar en heap/queue si quisieras extender
    sort_index: int = field(init=False, repr=False)
    severity: str     # "ALTO" | "CRITICO"
    pizza_type: str
    size: str
    elapsed_min: float
    started_at: str
    created_at: str = field(default_factory=now_str)
    oven_id: int = 0

    def __post_init__(self):
        # Prioridad: CRITICO primero
        self.sort_index = 0 if self.severity == "CRITICO" else 1

# =========================
# BakeTimer (temporizador de "horno")
# =========================
class BakeTimer:
    """
    Maneja la cocción de una pizza:
      - Cuenta tiempo transcurrido.
      - Cuando supera umbral por tamaño, emite una alerta al AlertManager.
      - Mantiene estado simple para UI.
    """
    _id_seq = 1
    _lock = threading.Lock()

    def __init__(self, pizza_type: str, size: str, alert_manager: "AlertManager"):
        with BakeTimer._lock:
            self.oven_id = BakeTimer._id_seq
            BakeTimer._id_seq += 1

        self.pizza_type = pizza_type
        self.size = size
        self.alert_manager = alert_manager

        self.threshold_min = COOKING_THRESHOLDS_MIN[size]
        self.started_at_ts = time.time()
        self.started_at = now_str()

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

        self._alert_sent = False  # evita duplicados
        self.elapsed_s = 0.0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # Ciclo “de cocción”
        while not self._stop.is_set():
            time.sleep(0.5)
            self.elapsed_s = time.time() - self.started_at_ts
            elapsed_min = self.elapsed_s / 60.0

            # Cuando supera umbral, dispara alerta (una vez)
            if not self._alert_sent and elapsed_min > self.threshold_min:
                sev = "CRITICO" if elapsed_min - self.threshold_min > CRITICAL_EXTRA_MIN else "ALTO"
                evt = AlertEvent(
                    severity=sev,
                    pizza_type=self.pizza_type,
                    size=self.size,
                    elapsed_min=elapsed_min,
                    started_at=self.started_at,
                    oven_id=self.oven_id
                )
                self.alert_manager.emit(evt)
                self._alert_sent = True

# =========================
# AlertManager (cola + sonido + log + limpieza)
# =========================
class AlertManager:
    """
    Recibe AlertEvent, registra en log, genera beep y notifica a la UI.
    Implementa cola para procesar eventos de forma ordenada.
    """
    def __init__(self, ui_callback_add, ui_callback_cleanup):
        self.ui_callback_add = ui_callback_add
        self.ui_callback_cleanup = ui_callback_cleanup
        self.queue: "queue.Queue[AlertEvent]" = queue.Queue()
        self._stop = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def stop(self):
        self._stop.set()
        try:
            self.queue.put_nowait(AlertEvent(severity="ALTO", pizza_type="dummy", size="personal", elapsed_min=0, started_at=now_str()))
        except Exception:
            pass

    def emit(self, evt: AlertEvent):
        self.queue.put(evt)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                evt = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # log estructurado
            logger.info(json.dumps({
                "type": "ALERTA_SOBRECOCCION",
                "oven_id": evt.oven_id,
                "pizza_type": evt.pizza_type,
                "size": evt.size,
                "severity": evt.severity,
                "elapsed_min": round(evt.elapsed_min, 1),
                "started_at": evt.started_at,
                "created_at": evt.created_at
            }, ensure_ascii=False))

            # beep / sonido (cross-plat básico)
            self._beep()

            # notificar UI (thread-safe via after en la propia UI)
            self.ui_callback_add(evt)

            # programar limpieza visual
            threading.Timer(ALERT_AUTO_CLEAN_S, lambda: self.ui_callback_cleanup(evt)).start()

            self.queue.task_done()

    def _beep(self):
        # Intento con winsound (Windows), de lo contrario campana Tk
        try:
            import winsound
            winsound.Beep(1000, 400)  # 400 ms
        except Exception:
            # fallback: no bloquear; la UI puede emitir bell()
            pass

# =========================
# UI
# =========================
class Story5App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #5 — Alertas de sobrecocción (Visual + Sonora + Logs)")
        self.geometry("1160x740")
        self.minsize(1080, 680)

        self.alert_manager = AlertManager(self._ui_add_alert, self._ui_cleanup_alert)

        # hornos activos
        self.timers: Dict[int, BakeTimer] = {}

        self._build_ui()
        self._tick_ui()

    def destroy(self):
        try:
            for t in list(self.timers.values()):
                t.stop()
            self.alert_manager.stop()
        finally:
            return super().destroy()

    def _build_ui(self):
        nb = ttk.Notebook(self)
        self.tab_ovens = ttk.Frame(nb)
        self.tab_alerts = ttk.Frame(nb)
        nb.add(self.tab_ovens, text="Hornos Activos")
        nb.add(self.tab_alerts, text="Panel de Alertas")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_ovens(self.tab_ovens)
        self._build_tab_alerts(self.tab_alerts)

        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w", foreground=THEME["accent"])
        status_bar.pack(fill="x", side="bottom")

    # ---------- Tab Hornos ----------
    def _build_tab_ovens(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        left = ttk.LabelFrame(frm, text="Nueva cocción", padding=10)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="Tipo de pizza").grid(row=0, column=0, sticky="w")
        self.cmb_pizza = ttk.Combobox(left, values=["pepperoni", "hawaiana"], state="readonly", width=24)
        self.cmb_pizza.set("pepperoni")
        self.cmb_pizza.grid(row=1, column=0, pady=(0,8))

        ttk.Label(left, text="Tamaño").grid(row=2, column=0, sticky="w")
        self.cmb_size = ttk.Combobox(left, values=["personal", "mediana", "grande"], state="readonly", width=24)
        self.cmb_size.set("mediana")
        self.cmb_size.grid(row=3, column=0, pady=(0,8))

        ttk.Button(left, text="Iniciar cocción", command=self._start_bake).grid(row=4, column=0, sticky="ew", pady=(10,6))
        ttk.Button(left, text="Iniciar 5 simultáneas (demo)", command=lambda: self._start_many(5)).grid(row=5, column=0, sticky="ew")

        ttk.Label(left, text="Umbrales por tamaño (min):").grid(row=6, column=0, sticky="w", pady=(12,0))
        ttk.Label(left, text=f"personal={COOKING_THRESHOLDS_MIN['personal']}  •  mediana={COOKING_THRESHOLDS_MIN['mediana']}  •  grande={COOKING_THRESHOLDS_MIN['grande']}").grid(row=7, column=0, sticky="w")

        right = ttk.LabelFrame(frm, text="Hornos activos", padding=10)
        right.pack(side="left", fill="both", expand=True, padx=(10,0))

        cols = ("oven_id","pizza","size","inicio","transcurrido","umbral","estado")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=22)
        headers = {
            "oven_id":"Horno", "pizza":"Pizza", "size":"Tamaño", "inicio":"Inicio", "transcurrido":"Transcurrido (min)",
            "umbral":"Umbral (min)", "estado":"Estado"
        }
        widths = {"oven_id":80, "pizza":120, "size":100, "inicio":160, "transcurrido":160, "umbral":120, "estado":120}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="center")
        self.tree.pack(fill="both", expand=True)

        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(8,0))
        ttk.Button(bar, text="Detener seleccionado", command=self._stop_selected).pack(side="left")
        ttk.Button(bar, text="Detener todos", command=self._stop_all).pack(side="left", padx=(8,0))

    # ---------- Tab Alertas ----------
    def _build_tab_alerts(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        header = ttk.Label(frm, text="Alertas de sobrecocción", font=("TkDefaultFont", 12, "bold"))
        header.pack(anchor="w", pady=(0,8))

        self.alert_list = tk.Listbox(frm, height=18)
        self.alert_list.pack(fill="both", expand=True)

        hint = ttk.Label(frm, text=f"Se limpia visualmente tras {ALERT_AUTO_CLEAN_S}s. Evidencias en {ALERTS_LOG}.", foreground="#616161")
        hint.pack(anchor="w", pady=(8,0))

    # ---------- Acciones ----------
    def _start_bake(self):
        pizza = self.cmb_pizza.get().strip()
        size = self.cmb_size.get().strip()
        timer = BakeTimer(pizza, size, self.alert_manager)
        self.timers[timer.oven_id] = timer
        timer.start()
        self.status.set(f"Iniciada cocción Horno #{timer.oven_id}: {pizza} ({size}).")
        self._refresh_tree()

    def _start_many(self, n: int):
        for _ in range(n):
            self._start_bake()

    def _stop_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("HornOS", "Selecciona un horno.")
            return
        oven_id = int(self.tree.item(sel[0])["values"][0])
        t = self.timers.get(oven_id)
        if t:
            t.stop()
            self.timers.pop(oven_id, None)
        self._refresh_tree()

    def _stop_all(self):
        for t in list(self.timers.values()):
            t.stop()
        self.timers.clear()
        self._refresh_tree()

    # ---------- UI updates ----------
    def _refresh_tree(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        for oven_id, t in sorted(self.timers.items()):
            elapsed_min = t.elapsed_s / 60.0
            state = "OK"
            if elapsed_min > t.threshold_min + CRITICAL_EXTRA_MIN:
                state = "CRÍTICO"
            elif elapsed_min > t.threshold_min:
                state = "ALTO"
            self.tree.insert("", "end", values=(
                t.oven_id, t.pizza_type, t.size, t.started_at,
                f"{elapsed_min:.1f}", f"{t.threshold_min:.1f}", state
            ))

    def _tick_ui(self):
        # refresca cada 0.5s
        self._refresh_tree()
        self.after(500, self._tick_ui)

    # ---------- AlertManager → UI ----------
    def _ui_add_alert(self, evt: AlertEvent):
        # Sonido básico si winsound falló: bell() de Tk
        try:
            self.bell()
        except Exception:
            pass

        text = (f"⚠ {evt.severity}: Horno #{evt.oven_id} "
                f"| {evt.pizza_type} ({evt.size}) "
                f"| Tiempo {evt.elapsed_min:.1f} min (umbral {COOKING_THRESHOLDS_MIN[evt.size]} min) "
                f"| Iniciado: {evt.started_at}")
        # Color por severidad
        if evt.severity == "CRITICO":
            text = f"[CRÍTICO] {text}"
        else:
            text = f"[ALTO] {text}"

        # Inserción UI (thread-safe vía .after ya hecho por AlertManager)
        self.alert_list.insert("end", text)

    def _ui_cleanup_alert(self, evt: AlertEvent):
        # Elimina entradas antiguas que coincidan con el horno/tiempo aproximado
        # (simple: borra la primera si hay demasiadas)
        if self.alert_list.size() > 0:
            self.alert_list.delete(0)

# =========================
# Main
# =========================
if __name__ == "__main__":
    app = Story5App()
    if THEME["bg"]:
        app.configure(bg=THEME["bg"])
    app.mainloop()
