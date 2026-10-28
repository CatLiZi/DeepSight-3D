"""Read USB hub connection information with GET-only IOCTLs (Microsoft USBView API)."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import struct
import uuid


class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]


class InterfaceData(C.Structure):
    _fields_ = [('size', W.DWORD), ('guid', GUID), ('flags', W.DWORD), ('reserved', C.c_void_p)]


def main():
    setup = C.WinDLL('setupapi', use_last_error=True)
    kernel = C.WinDLL('kernel32', use_last_error=True)
    setup.SetupDiGetClassDevsW.argtypes = [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD]
    setup.SetupDiGetClassDevsW.restype = W.HANDLE
    setup.SetupDiEnumDeviceInterfaces.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(GUID), W.DWORD, C.POINTER(InterfaceData)]
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [W.HANDLE, C.POINTER(InterfaceData), C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [W.HANDLE]
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    guid = GUID((C.c_ubyte * 16).from_buffer_copy(uuid.UUID('f18a0e88-c30c-11d0-8815-00a0c906bed8').bytes_le))
    device_set = setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 18)
    invalid = C.c_void_p(-1).value
    if device_set == invalid:
        raise C.WinError(C.get_last_error())
    found, errors = [], []
    try:
        for index in range(128):
            interface = InterfaceData()
            interface.size = C.sizeof(interface)
            if not setup.SetupDiEnumDeviceInterfaces(device_set, None, C.byref(guid), index, C.byref(interface)):
                break
            detail = C.create_string_buffer(4096)
            struct.pack_into('<I', detail, 0, 8 if C.sizeof(C.c_void_p) == 8 else 6)
            required = W.DWORD()
            if not setup.SetupDiGetDeviceInterfaceDetailW(device_set, C.byref(interface), detail, len(detail), C.byref(required), None):
                errors.append({'interface': index, 'error': C.get_last_error()})
                continue
            path = C.wstring_at(C.addressof(detail) + 4)
            handle = kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
            if handle == invalid:
                errors.append({'hub': index, 'error': C.get_last_error()})
                continue
            try:
                node = C.create_string_buffer(512)
                returned = W.DWORD()
                if not kernel.DeviceIoControl(handle, 0x220408, node, 512, node, 512, C.byref(returned), None):
                    errors.append({'hub': index, 'node_error': C.get_last_error()})
                    continue
                ports = node.raw[6]
                for port in range(1, ports + 1):
                    info = C.create_string_buffer(4096)
                    struct.pack_into('<I', info, 0, port)
                    if not kernel.DeviceIoControl(handle, 0x220448, info, 4096, info, 4096, C.byref(returned), None):
                        continue
                    vid, pid = struct.unpack_from('<HH', info.raw, 12)
                    if (vid, pid) not in ((0x1BCF, 0x0B15), (0xEF09, 0x459D)):
                        continue
                    bcd_usb, bcd_device = struct.unpack_from('<H', info.raw, 6)[0], struct.unpack_from('<H', info.raw, 16)[0]
                    speed_code = info.raw[23]
                    v2 = C.create_string_buffer(16)
                    struct.pack_into('<III', v2, 0, port, 16, 7)
                    v2_ok = kernel.DeviceIoControl(handle, 0x22045C, v2, 16, v2, 16, C.byref(returned), None)
                    flags = struct.unpack_from('<I', v2.raw, 12)[0] if v2_ok else None
                    found.append({'vid': f'{vid:04X}', 'pid': f'{pid:04X}', 'hub_index': index, 'port': port,
                                  'usb_bcd': f'{bcd_usb:04X}', 'device_bcd': f'{bcd_device:04X}',
                                  'speed_code': speed_code, 'speed': ['LowSpeed', 'FullSpeed', 'HighSpeed', 'SuperSpeed'][min(speed_code, 3)],
                                  'connection_info_v2_flags': flags})
            finally:
                kernel.CloseHandle(handle)
    finally:
        setup.SetupDiDestroyDeviceInfoList(device_set)
    report = {'method': 'USB hub GET_NODE_CONNECTION_INFORMATION_EX and EX_V2; read-only', 'devices': found, 'errors': errors}
    path = Path(__file__).resolve().parents[2] / 'hardware_inventory/stage1/usb-speed.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
