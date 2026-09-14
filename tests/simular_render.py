"""Simula el plan Free de Render (0,1 de CPU) y corre la prueba de carga. Solo Windows.

No la corre pytest. Uso, desde la raiz del repositorio:

    python tests/simular_render.py 10

Levanta el tablero en modo parquet (0 egress) dentro de un Job Object de
Windows con la CPU limitada a 0,1 nucleos, espera a que responda, corre
tests/prueba_carga.py y reporta la CPU usada y el pico de memoria del servidor.

Sobre la memoria: no se limita, se mide. En Windows el limite de un Job Object
aplica a la memoria reservada y no a la usada, y las librerias numericas
reservan mucho mas de lo que usan, asi que el servidor moria al importar. Render
(Linux) limita la memoria usada, que es la que se reporta aca (pico de working
set) para comparar contra sus 512 MB. Es una aproximacion: Linux puede usar algo
mas o algo menos.
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if sys.platform != "win32":
    sys.exit("Esta simulacion usa Job Objects de Windows.")

import ctypes.wintypes as wt  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
CPU_RENDER = 0.1
MEMORIA_RENDER_MB = 512
USUARIOS = sys.argv[1] if len(sys.argv) > 1 else "10"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi")


class LimiteBasico(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class LimiteExtendido(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", LimiteBasico), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class TopeCPU(ctypes.Structure):
    _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]


class Contabilidad(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]


class MemoriaProceso(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t)] + [(f"_{i}", ctypes.c_size_t) for i in range(6)]


k32.CreateJobObjectW.restype = wt.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(MemoriaProceso), wt.DWORD]


def exigir(ok, que):
    if not ok:
        raise OSError(ctypes.get_last_error(), que)


def cpu_total(job):
    c = Contabilidad()
    k32.QueryInformationJobObject(job, 1, ctypes.byref(c), ctypes.sizeof(c), None)
    return (c.TotalUserTime + c.TotalKernelTime) / 1e7


def main():
    job = k32.CreateJobObjectW(None, None)
    exigir(job, "crear el Job Object")
    limite = LimiteExtendido()
    limite.BasicLimitInformation.LimitFlags = 0x2000  # cerrar los procesos al terminar
    exigir(k32.SetInformationJobObject(job, 9, ctypes.byref(limite), ctypes.sizeof(limite)), "configurar el Job")
    tope = TopeCPU(ControlFlags=0x1 | 0x4, CpuRate=max(1, round(CPU_RENDER / os.cpu_count() * 10000)))
    exigir(k32.SetInformationJobObject(job, 15, ctypes.byref(tope), ctypes.sizeof(tope)), "limitar la CPU")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    entorno = {**os.environ, "FUENTE_DATOS": "parquet", "POLARS_MAX_THREADS": "2", "PYTHONUNBUFFERED": "1",
               "SUPABASE_DASHBOARD_DB_URL": "postgresql://sin_red:sin_red@127.0.0.1:1/sin_red"}
    registro = open(RAIZ / "tests" / "simular_render.log", "w", encoding="utf-8")
    t0 = time.time()
    servidor = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(puerto)],
        cwd=RAIZ, env=entorno, stdout=registro, stderr=subprocess.STDOUT)
    exigir(k32.AssignProcessToJobObject(job, int(servidor._handle)), "asignar el servidor al Job")

    try:
        url = f"http://127.0.0.1:{puerto}"
        while True:
            try:
                urllib.request.urlopen(url + "/", timeout=5).read()
                break
            except OSError:
                if servidor.poll() is not None:
                    sys.exit(f"El servidor murio al arrancar; ver tests/simular_render.log")
                time.sleep(1)
        print(f"Arranque en frio con {CPU_RENDER} de CPU: {time.time() - t0:.0f}s hasta responder")

        cpu_antes, t_carga = cpu_total(job), time.time()
        subprocess.run([sys.executable, str(RAIZ / "tests" / "prueba_carga.py"), url, USUARIOS], cwd=RAIZ)
        duracion = time.time() - t_carga

        memoria = MemoriaProceso(cb=ctypes.sizeof(MemoriaProceso))
        psapi.GetProcessMemoryInfo(wt.HANDLE(int(servidor._handle)), ctypes.byref(memoria), memoria.cb)
        pico_mb = memoria.PeakWorkingSetSize / 2**20
        print(f"CPU usada durante la carga: {(cpu_total(job) - cpu_antes) / duracion:.3f} nucleos (tope {CPU_RENDER})")
        print(f"Pico de memoria del servidor: {pico_mb:.0f} MB (Render: {MEMORIA_RENDER_MB} MB)")
        print(f"Servidor vivo al final: {servidor.poll() is None}")
    finally:
        servidor.terminate()
        registro.close()


if __name__ == "__main__":
    main()
