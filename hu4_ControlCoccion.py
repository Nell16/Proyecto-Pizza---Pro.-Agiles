"""
Historia #4 — Control automático de cocción
Cubre (según documentación):
- Simulación de sensores de temperatura/tiempo con lecturas periódicas.
- Ajuste automático de parámetros de cocción cuando hay desviaciones.
- Detección de fallos de sensor (sin lecturas / fuera de rango) + alerta (visual + log).
- Registro de eventos (ajustes y fallos) en log para control interno.
- Métricas de rendimiento (eficiencia térmica, eventos, ajustes) y generación de reporte.
- Sesiones de simulación prolongadas (demo corta en GUI), sin bloqueos.

Arquitectura escalable:
- Repositorio/constantes independientes.
- Servicio CookingController (lógica de sensores y control).
- UI desacoplada con callbacks thread-safe y Canvas (para un gráfico simple).
"""

import os
import json
import time
import math
import random
import logging
from logging.handlers import RotatingFileHandler
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List

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

COCCION_LOG = os.path.join(LOGS_DIR, "coccion.log")

THEME = {
    "accent": "#1b5e20",
    "warn": "#e65100",
    "ok": "#2e7d32",
    "bad": "#b71c1c",
}

# Tamaños → umbrales de tiempo (minutos); útil para story #5, aquí mantenemos referencia
COOKING_THRESHOLDS_MIN = {"personal": 10, "mediana": 12, "grande": 15}

# Objetivo térmico por tipo (ejemplo base; puedes mover a BD luego)
TARGET_TEMP_BY_PIZZA = {
    "pepperoni": 230.0,
    "hawaiana": 225.0,
}

# Ventana de control (|temp - target| <= TOL) considerada "en rango"
TEMP_TOLERANCE_DEG = 2.0

# Intervalos y límites de demo
SENSOR_PERIOD_S = 0.5            # lectura cada 0.5s
SENSOR_FAIL_PROB = 0.03          # 3% para simular fallos (puedes ajustar)
SENSOR_OUTOFRANGE_PROB = 0.02    # lecturas “locas” ocasionales
MAX_SESSION_SECONDS = 60 * 10    # tope safety

# =========================
# Logging
# =========================
logger = logging.getLogger("story4_coccion")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = RotatingFileHandler(COCCION_LOG, maxBytes=512_000, backupCount=2)
    _fmt = logging.Formatter("[%(asctime)s] %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =========================
# DTOs / Reportes
# =========================
@dataclass
class TelemetryPoint:
    t: float
    temp: float
    target: float

@dataclass
class SessionReport:
    pizza_type: str
    size: str
    started_at: str
    duration_s: int
    target_temp: float
    efficiency_pct: float
    adjustments: int
    events_count: int
    sensor_failures: int
    notes: str = ""
    telemetry: List[Dict[str, float]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "pizza_type": self.pizza_type,
            "size": self.size,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "target_temp": self.target_temp,
            "efficiency_pct": round(self.efficiency_pct, 1),
            "adjustments": self.adjustments,
            "events_count": self.events_count,
            "sensor_failures": self.sensor_failures,
            "notes": self.notes,
            "telemetry": self.telemetry,
        }, ensure_ascii=False, indent=2)

# =========================
# Servicio de Control
# =========================
class CookingController:
    """
    Simula:
      - Sensor de temperatura con ruido.
      - Ajuste automático si |temp-target| > TEMP_TOLERANCE_DEG.
      - Fallos de sensor (sin lectura) y lecturas fuera de rango.
      - Métricas: eficiencia térmica (% tiempo en rango), #ajustes, #eventos log.
      - Callback UI para actualizar en tiempo real y para alertas de sensor.
      - Reporte final de la sesión.
    """
    def __init__(
        self,
        pizza_type: str,
        size: str,
        duration_s: int,
        ui_on_sample: Callable[[TelemetryPoint], None],
        ui_on_event: Callable[[str, str], None],
        ui_on_done: Callable[[SessionReport], None],
        target_override: Optional[float] = None,
    ):
        self.pizza_type = pizza_type
        self.size = size
        self.duration_s = min(duration_s, MAX_SESSION_SECONDS)
        self.target_temp = target_override if target_override is not None else TARGET_TEMP_BY_PIZZA.get(pizza_type, 228.0)

        self.ui_on_sample = ui_on_sample
        self.ui_on_event = ui_on_event
        self.ui_on_done = ui_on_done

        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

        # Estado de control
        self._current_temp = self.target_temp + random.uniform(-1.0, 1.0)
        self._eff_inrange_time = 0.0
        self._adjustments = 0
        self._events = 0
        self._sensor_failures = 0
        self._telemetry: List[TelemetryPoint] = []
        self._start_ts: Optional[float] = None

    def start(self):
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()

    def _emit_sample(self, t_rel: float, sensor_temp: float):
        pt = TelemetryPoint(t=t_rel, temp=sensor_temp, target=self.target_temp)
        self._telemetry.append(pt)
        self.ui_on_sample(pt)
        # log “telemetría cruda”
        logger.info(json.dumps({"type":"telemetry", "t": round(t_rel,2), "temp": round(sensor_temp,2), "target": self.target_temp}))

    def _emit_event(self, level: str, msg: str):
        self._events += 1
        # level: INFO/WARN/ERROR
        logger.info(f"{level} | {msg}")
        self.ui_on_event(level, msg)

    def _run(self):
        self._start_ts = time.time()
        started_at_str = now_str()
        self._emit_event("INFO", f"Iniciando cocción: {self.pizza_type} ({self.size}) | objetivo {self.target_temp}°C")

        elapsed = 0.0
        last_sample = 0.0

        while not self._stop.is_set() and elapsed < self.duration_s:
            time.sleep(SENSOR_PERIOD_S)
            elapsed = time.time() - self._start_ts

            # Simulación de fallo de sensor: no se emite lectura
            if random.random() < SENSOR_FAIL_PROB:
                self._sensor_failures += 1
                self._emit_event("WARN", "Fallo de sensor: sin lectura en este intervalo.")
                continue

            # Lectura con ruido
            sensor_temp = self._current_temp + random.uniform(-2.5, 2.5)

            # Lectura fuera de rango (sensor “loco”)
            if random.random() < SENSOR_OUTOFRANGE_PROB:
                sensor_temp += random.choice([-1, 1]) * random.uniform(8.0, 15.0)
                self._emit_event("WARN", f"Lectura fuera de rango: {sensor_temp:.1f}°C")

            # Control sencillo: ajuste si difiere > tolerancia
            if abs(sensor_temp - self.target_temp) > TEMP_TOLERANCE_DEG:
                # Ajuste proporcional simple (step pequeño)
                self._current_temp += (self.target_temp - sensor_temp) * 0.2
                self._adjustments += 1
                self._emit_event("INFO", f"Ajuste automático #{self._adjustments}: nueva base {self._current_temp:.1f}°C")

            # Eficiencia: tiempo en rango
            if abs(sensor_temp - self.target_temp) <= TEMP_TOLERANCE_DEG:
                self._eff_inrange_time += SENSOR_PERIOD_S

            # Emitir muestra (UI + log)
            last_sample = elapsed
            self._emit_sample(last_sample, sensor_temp)

        # Cierre sesión
        eff_pct = (self._eff_inrange_time / max(1.0, self.duration_s)) * 100.0
        report = SessionReport(
            pizza_type=self.pizza_type,
            size=self.size,
            started_at=started_at_str,
            duration_s=int(self.duration_s),
            target_temp=self.target_temp,
            efficiency_pct=eff_pct,
            adjustments=self._adjustments,
            events_count=self._events,
            sensor_failures=self._sensor_failures,
            telemetry=[{"t": round(p.t,2), "temp": round(p.temp,2), "target": p.target} for p in self._telemetry]
        )
        logger.info(json.dumps({
            "type":"session_end",
            "pizza": self.pizza_type,
            "size": self.size,
            "duration_s": self.duration_s,
            "efficiency_pct": round(eff_pct,1),
            "adjustments": self._adjustments,
            "events": self._events,
            "sensor_failures": self._sensor_failures
        }))
        self.ui_on_done(report)

# =========================
# UI
# =========================
class Story4App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Historia #4 — Control automático de cocción (Sensores, Ajustes, Métricas)")
        self.geometry("1160x760")
        self.minsize(1080, 700)

        self.controller: Optional[CookingController] = None
        self.telemetry_points: List[TelemetryPoint] = []

        self._build_ui()

    def _build_ui(self):
        nb = ttk.Notebook(self)
        self.tab_ctrl = ttk.Frame(nb)
        self.tab_metrics = ttk.Frame(nb)
        nb.add(self.tab_ctrl, text="Control y Telemetría")
        nb.add(self.tab_metrics, text="Métricas y Reporte")
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_tab_control(self.tab_ctrl)
        self._build_tab_metrics(self.tab_metrics)

        self.status = tk.StringVar(value="Listo.")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w", foreground=THEME["accent"])
        status_bar.pack(fill="x", side="bottom")

    # ---------- Tab Control ----------
    def _build_tab_control(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        # Config izquierda
        left = ttk.LabelFrame(frm, text="Parámetros de cocción", padding=10)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="Tipo de pizza").grid(row=0, column=0, sticky="w")
        self.cmb_pizza = ttk.Combobox(left, values=["pepperoni", "hawaiana"], state="readonly", width=22)
        self.cmb_pizza.set("pepperoni")
        self.cmb_pizza.grid(row=1, column=0, pady=(0,8))

        ttk.Label(left, text="Tamaño").grid(row=2, column=0, sticky="w")
        self.cmb_size = ttk.Combobox(left, values=["personal", "mediana", "grande"], state="readonly", width=22)
        self.cmb_size.set("mediana")
        self.cmb_size.grid(row=3, column=0, pady=(0,8))

        ttk.Label(left, text="Duración (segundos)").grid(row=4, column=0, sticky="w")
        self.ent_duration = ttk.Entry(left, width=24)
        self.ent_duration.insert(0, "30")  # demo corta
        self.ent_duration.grid(row=5, column=0, pady=(0,8))

        ttk.Label(left, text="Objetivo °C (opcional)").grid(row=6, column=0, sticky="w")
        self.ent_target = ttk.Entry(left, width=24)
        self.ent_target.insert(0, "")  # usa default por tipo
        self.ent_target.grid(row=7, column=0, pady=(0,8))

        self.btn_start = ttk.Button(left, text="Iniciar simulación", command=self._start_sim)
        self.btn_start.grid(row=8, column=0, sticky="ew", pady=(8,4))
        self.btn_stop = ttk.Button(left, text="Detener", command=self._stop_sim, state="disabled")
        self.btn_stop.grid(row=9, column=0, sticky="ew")

        ttk.Label(left, text=f"Ventana 'en rango': ±{TEMP_TOLERANCE_DEG}°C").grid(row=10, column=0, sticky="w", pady=(10,0))

        # Centro: Telemetría (consola) + eventos
        center = ttk.LabelFrame(frm, text="Telemetría (cada 0.5 s) y eventos", padding=10)
        center.pack(side="left", fill="both", expand=True, padx=(10,0))

        self.txt_console = tk.Text(center, height=26)
        self.txt_console.pack(fill="both", expand=True)

        # Derecha: Gráfico simple en Canvas
        right = ttk.LabelFrame(frm, text="Gráfico (temp vs tiempo)", padding=10)
        right.pack(side="left", fill="both", expand=True, padx=(10,0))

        self.canvas = tk.Canvas(right, width=500, height=320, bg="#fafafa", highlightthickness=1, highlightbackground="#ddd")
        self.canvas.pack(fill="both", expand=True)

        # leyenda
        self.lbl_graph_legend = ttk.Label(right, text="— temp | — target", foreground="#616161")
        self.lbl_graph_legend.pack(anchor="w", pady=(6,0))

    # ---------- Tab Métricas / Reporte ----------
    def _build_tab_metrics(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)

        self.metrics_text = tk.Text(frm, height=28)
        self.metrics_text.pack(fill="both", expand=True)

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(10,0))
        ttk.Button(bar, text="Exportar reporte JSON", command=self._export_report).pack(side="left")
        ttk.Button(bar, text="Limpiar métrica actual", command=self._clear_report).pack(side="left", padx=(8,0))
        ttk.Label(bar, text=f"Log: {COCCION_LOG}", foreground="#616161").pack(side="left", padx=(12,0))

        self._last_report: Optional[SessionReport] = None

    # ---------- Control handlers ----------
    def _start_sim(self):
        if self.controller:
            messagebox.showwarning("Simulación", "Ya hay una simulación en curso.")
            return

        pizza = self.cmb_pizza.get().strip()
        size = self.cmb_size.get().strip()

        try:
            duration_s = int(self.ent_duration.get().strip())
        except ValueError:
            messagebox.showerror("Entrada", "Duración inválida (usa segundos, entero).")
            return
        if duration_s <= 0:
            messagebox.showerror("Entrada", "La duración debe ser > 0.")
            return

        target_override = None
        if self.ent_target.get().strip():
            try:
                target_override = float(self.ent_target.get().strip())
            except ValueError:
                messagebox.showerror("Entrada", "Objetivo °C inválido.")
                return

        # limpiar UI
        self.txt_console.delete("1.0", tk.END)
        self.telemetry_points.clear()
        self._clear_canvas()

        # crear controller
        self.controller = CookingController(
            pizza_type=pizza,
            size=size,
            duration_s=duration_s,
            ui_on_sample=self._ui_on_sample,
            ui_on_event=self._ui_on_event,
            ui_on_done=self._ui_on_done,
            target_override=target_override
        )
        self.controller.start()
        self.status.set(f"Simulación iniciada: {pizza} ({size}) por {duration_s}s.")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def _stop_sim(self):
        if not self.controller:
            return
        self.controller.stop()
        self.status.set("Solicitando detener simulación…")

    # ---------- UI callbacks (thread-safe via .after) ----------
    def _ui_on_sample(self, pt: TelemetryPoint):
        self.after(0, lambda: self._append_sample_ui(pt))

    def _ui_on_event(self, level: str, msg: str):
        self.after(0, lambda: self._append_event_ui(level, msg))

    def _ui_on_done(self, report: SessionReport):
        self.after(0, lambda: self._finish_ui(report))

    # ---------- UI update impl ----------
    def _append_sample_ui(self, pt: TelemetryPoint):
        # texto
        self.txt_console.insert("end", f"[t={pt.t:5.1f}s] temp={pt.temp:6.2f}°C target={pt.target:.1f}°C\n")
        self.txt_console.see("end")
        # vector para graf
        self.telemetry_points.append(pt)
        self._draw_graph()

    def _append_event_ui(self, level: str, msg: str):
        color = {
            "INFO": THEME["ok"],
            "WARN": THEME["warn"],
            "ERROR": THEME["bad"]
        }.get(level, "#333")
        self.txt_console.insert("end", f"{level}: {msg}\n")
        self.txt_console.tag_add(level, "end-1l linestart", "end-1l lineend")
        self.txt_console.tag_config(level, foreground=color)
        self.txt_console.see("end")

    def _finish_ui(self, report: SessionReport):
        self._last_report = report
        self.status.set(f"Sesión finalizada. Eficiencia {report.efficiency_pct:.1f}% | Ajustes {report.adjustments} | Fallos {report.sensor_failures}")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.controller = None
        # pintar métricas
        self._paint_metrics(report)

    # ---------- Canvas chart ----------
    def _clear_canvas(self):
        self.canvas.delete("all")
        # eje básico
        w = self.canvas.winfo_width() or 500
        h = self.canvas.winfo_height() or 320
        self.canvas.create_line(40, h-30, w-10, h-30, fill="#bbb")  # eje X
        self.canvas.create_line(40, 10, 40, h-30, fill="#bbb")      # eje Y

    def _draw_graph(self):
        self._clear_canvas()
        if not self.telemetry_points:
            return
        w = self.canvas.winfo_width() or 500
        h = self.canvas.winfo_height() or 320

        # escalas
        T = max(1.0, self.telemetry_points[-1].t)
        temps = [p.temp for p in self.telemetry_points]
        tmin = min(min(temps), self.telemetry_points[-1].target - 10)
        tmax = max(max(temps), self.telemetry_points[-1].target + 10)
        # márgenes
        left, right, top, bottom = 40, 10, 10, 30
        xspan = (w - left - right)
        yspan = (h - top - bottom)

        def xmap(t): return left + (t / T) * xspan
        def ymap(val):
            # invertir porque canvas y crece hacia abajo
            return top + (tmax - val) / max(1e-6, (tmax - tmin)) * yspan

        # dibujar target como línea horizontal
        target = self.telemetry_points[-1].target
        self.canvas.create_line(left, ymap(target), left + xspan, ymap(target), fill="#888")

        # dibujar serie de temperatura
        last = None
        for p in self.telemetry_points:
            x = xmap(p.t)
            y = ymap(p.temp)
            if last is not None:
                self.canvas.create_line(last[0], last[1], x, y, fill="#3f51b5")
            last = (x, y)

    # ---------- Métricas / Reporte ----------
    def _paint_metrics(self, report: SessionReport):
        self.metrics_text.delete("1.0", tk.END)
        s = [
            f"Pizza         : {report.pizza_type}",
            f"Tamaño        : {report.size}",
            f"Inició        : {report.started_at}",
            f"Duración (s)  : {report.duration_s}",
            f"Objetivo (°C) : {report.target_temp}",
            f"Eficiencia (%) : {report.efficiency_pct:.1f}",
            f"Ajustes       : {report.adjustments}",
            f"Eventos       : {report.events_count}",
            f"Fallos sensor : {report.sensor_failures}",
            "",
            "Notas         : " + (report.notes or "-"),
            "",
            "Tip: Exporta el JSON para evidencias del Sprint Review."
        ]
        self.metrics_text.insert("1.0", "\n".join(s))

    def _export_report(self):
        if not self._last_report:
            messagebox.showinfo("Reporte", "No hay reporte de sesión aún.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=f"reporte_coccion_{self._last_report.pizza_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._last_report.to_json())
        messagebox.showinfo("Reporte", f"Exportado: {os.path.basename(path)}")

    def _clear_report(self):
        self._last_report = None
        self.metrics_text.delete("1.0", tk.END)
        self.status.set("Métricas limpiadas.")

# =========================
# Main
# =========================
if __name__ == "__main__":
    app = Story4App()
    app.mainloop()
