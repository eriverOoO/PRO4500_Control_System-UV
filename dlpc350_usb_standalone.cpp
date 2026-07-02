#include <cstring>
#include <iomanip>
#include <sstream>
#include <string>

#include "GUI/dlpc350_usb.h"
#include "GUI/hidapi-master/hidapi/hidapi.h"
#include "projector_usb_diagnostics.h"

static hid_device* g_device = nullptr;
static int g_connected = 0;
static std::wstring g_lastError;

unsigned char g_OutputBuffer[USB_MAX_PACKET_SIZE + 1]{};
unsigned char g_InputBuffer[USB_MAX_PACKET_SIZE + 1]{};

int DLPC350_USB_IsConnected() {
    return g_connected;
}

int DLPC350_USB_Init() {
    const int result = hid_init();
    if (result != 0) {
        g_lastError = L"HIDAPI 초기화에 실패했습니다.";
    }
    return result;
}

int DLPC350_USB_Exit() {
    return hid_exit();
}

int DLPC350_USB_Open() {
    if (g_device) {
        hid_close(g_device);
        g_device = nullptr;
    }
    g_connected = 0;
    g_lastError.clear();

    // hid_open() only tries the first matching HID interface. Some Windows
    // installations expose more than one interface for the same VID/PID, so
    // try every matching path until one can actually be opened.
    hid_device_info* devices = hid_enumerate(MY_VID, 0);
    int matchingDevices = 0;
    int tiDevices = 0;
    std::wostringstream detectedProducts;

    for (hid_device_info* device = devices; device; device = device->next) {
        ++tiDevices;
        if (device->product_id != MY_PID) {
            detectedProducts << L" 0x"
                             << std::hex << std::uppercase
                             << std::setw(4) << std::setfill(L'0')
                             << device->product_id;
            continue;
        }

        ++matchingDevices;
        g_device = hid_open_path(device->path);
        if (g_device) {
            g_connected = 1;
            break;
        }
    }

    hid_free_enumeration(devices);

    if (g_connected) {
        return 0;
    }

    if (matchingDevices > 0) {
        g_lastError =
            L"LightCrafter 4500은 감지되었지만 HID 장치를 열 수 없습니다. "
            L"TI GUI를 종료하고, 장치 관리자에서 HID 드라이버 상태를 확인한 뒤 "
            L"프로그램을 관리자 권한으로 다시 실행해 보세요. 감지된 인터페이스: " +
            std::to_wstring(matchingDevices);
    } else if (tiDevices > 0) {
        g_lastError =
            L"TI USB 장치는 감지되었지만 LightCrafter 4500 PID 0x6401이 아닙니다."
            L" 감지된 PID:" + detectedProducts.str();
    } else {
        g_lastError =
            L"LightCrafter 4500 USB 장치를 찾지 못했습니다 (VID 0x0451, PID 0x6401). "
            L"전원, 데이터 통신이 가능한 USB 케이블, Windows 장치 관리자를 확인하세요.";
    }
    return -1;
}

int DLPC350_USB_Write() {
    if (!g_device) {
        return -1;
    }
    const int written = hid_write(g_device, g_OutputBuffer, USB_MIN_PACKET_SIZE + 1);
    if (written < 0) {
        hid_close(g_device);
        g_device = nullptr;
        g_connected = 0;
    }
    return written;
}

int DLPC350_USB_Read() {
    if (!g_device) {
        return -1;
    }
    std::memset(g_InputBuffer, 0, sizeof(g_InputBuffer));
    const int read = hid_read_timeout(g_device, g_InputBuffer, USB_MIN_PACKET_SIZE + 1, 2000);
    if (read < 0) {
        hid_close(g_device);
        g_device = nullptr;
        g_connected = 0;
    }
    return read;
}

int DLPC350_USB_Close() {
    if (g_device) {
        hid_close(g_device);
        g_device = nullptr;
    }
    g_connected = 0;
    return 0;
}

const std::wstring& DLPC350_USB_LastError() {
    return g_lastError;
}
