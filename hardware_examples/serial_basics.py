"""对照样例：枚举 COM 串口；直接运行只列设备，不打开串口或发送命令。"""

from __future__ import annotations

import sys

import serial
from serial.tools import list_ports


def exchange_command(port: str, baudrate: int, command: bytes) -> bytes:
    """串口通信写法示意：调用者必须先确认端口、波特率和命令协议。"""
    if not command:
        raise ValueError("Supply a command defined by the device protocol")
    with serial.Serial(port=port, baudrate=baudrate,
                       timeout=0.5, write_timeout=0.5) as connection:
        connection.write(command)
        connection.flush()
        return connection.read(64)  # 此处也应按真实协议改成对应的应答长度/拆包方式。


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ports = list(list_ports.comports())
    for port in ports:
        vid = f"{port.vid:04X}" if port.vid is not None else "unknown"
        pid = f"{port.pid:04X}" if port.pid is not None else "unknown"
        print(f"{port.device}: {port.description}; VID={vid}, PID={pid}")
        if (port.vid, port.pid) == (0xEF09, 0x459D):
            print("  YDPI4MIC board serial interface; protocol is not confirmed.")
    if not ports:
        print("No COM ports found")


if __name__ == "__main__":
    main()
