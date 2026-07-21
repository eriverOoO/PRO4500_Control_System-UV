#include <chrono>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <string>
#include <thread>
#include <utility>

#include "GUI/dlpc350_usb.h"
#include "GUI/hidapi-master/hidapi/hidapi.h"
#include "projector_usb_diagnostics.h"

static hid_device* g_device = nullptr;
static int g_connected = 0;
static std::wstring g_lastError;

namespace {

constexpr int kOpenRetryDelaysMs[] = {0, 150, 300, 600};

struct EnumerationObservation {
    int matchingDevices = 0;
    int vendorDevices = 0;
    std::wstring detectedProducts;
    std::wstring matchingInterfaces;
};

void close_device() {
    if (g_device) {
        hid_close(g_device);
        g_device = nullptr;
    }
    g_connected = 0;
}

void set_hid_error(const wchar_t* operation) {
    std::wstring message = operation;
    message += L" failed";
    if (g_device) {
        const wchar_t* detail = hid_error(g_device);
        if (detail && *detail) {
            message += L": ";
            message += detail;
        }
    }
    g_lastError = std::move(message);
}

bool open_once(EnumerationObservation& observation) {
    hid_device_info* devices = hid_enumerate(MY_VID, 0);
    std::wostringstream products;
    std::wostringstream interfaces;

    for (hid_device_info* device = devices; device; device = device->next) {
        ++observation.vendorDevices;
        if (device->product_id != MY_PID) {
            products << L" 0x"
                     << std::hex << std::uppercase
                     << std::setw(4) << std::setfill(L'0')
                     << device->product_id;
            continue;
        }

        ++observation.matchingDevices;
        interfaces << L" [interface=" << std::dec << device->interface_number
                   << L", usage_page=0x" << std::hex << std::uppercase
                   << device->usage_page << L", usage=0x" << device->usage << L"]";
        g_device = hid_open_path(device->path);
        if (g_device) {
            g_connected = 1;
            break;
        }
    }

    observation.detectedProducts = products.str();
    observation.matchingInterfaces = interfaces.str();
    hid_free_enumeration(devices);
    return g_connected != 0;
}

}  // namespace

unsigned char g_OutputBuffer[USB_MAX_PACKET_SIZE + 1]{};
unsigned char g_InputBuffer[USB_MAX_PACKET_SIZE + 1]{};

int DLPC350_USB_IsConnected() {
    return g_connected;
}

int DLPC350_USB_Init() {
    const int result = hid_init();
    if (result != 0) {
        g_lastError = L"HIDAPI initialization failed.";
    } else {
        g_lastError.clear();
    }
    return result;
}

int DLPC350_USB_Exit() {
    return hid_exit();
}

int DLPC350_USB_Open() {
    close_device();
    g_lastError.clear();

    // hid_open() only tries the first matching HID interface. Some Windows
    // installations expose more than one interface for the same VID/PID, so
    // try every matching path. Windows can briefly omit a HID interface while
    // the device powers up or immediately after another process closes it.
    EnumerationObservation bestObservation;
    for (int delayMs : kOpenRetryDelaysMs) {
        if (delayMs > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delayMs));
        }

        EnumerationObservation observation;
        if (open_once(observation)) {
            return 0;
        }
        if (observation.matchingDevices > bestObservation.matchingDevices
            || (observation.matchingDevices == bestObservation.matchingDevices
                && observation.vendorDevices > bestObservation.vendorDevices)) {
            bestObservation = std::move(observation);
        }
    }

    const int attempts = static_cast<int>(sizeof(kOpenRetryDelaysMs) / sizeof(kOpenRetryDelaysMs[0]));
    if (bestObservation.matchingDevices > 0) {
        g_lastError =
            L"LightCrafter 4500 was enumerated but no HID interface could be opened after "
            + std::to_wstring(attempts) + L" attempts. Close the TI GUI and any other process "
            L"using the projector, then check the HID driver. Detected interfaces: "
            + std::to_wstring(bestObservation.matchingDevices)
            + bestObservation.matchingInterfaces;
    } else if (bestObservation.vendorDevices > 0) {
        g_lastError =
            L"TI HID devices were found, but none used the LightCrafter 4500 PID 0x6401."
            L" Detected PIDs:" + bestObservation.detectedProducts;
    } else {
        g_lastError =
            L"LightCrafter 4500 was not enumerated after " + std::to_wstring(attempts)
            + L" attempts (VID 0x0451, PID 0x6401). Check projector power, a data-capable "
            L"USB cable, Windows Device Manager, and USB selective-suspend settings.";
    }
    return -1;
}

int DLPC350_USB_Write() {
    if (!g_device) {
        g_lastError = L"HID write requested without an open LightCrafter 4500 handle.";
        return -1;
    }
    const int written = hid_write(g_device, g_OutputBuffer, USB_MIN_PACKET_SIZE + 1);
    if (written < 0) {
        set_hid_error(L"HID write");
        close_device();
    }
    return written;
}

int DLPC350_USB_Read() {
    if (!g_device) {
        g_lastError = L"HID read requested without an open LightCrafter 4500 handle.";
        return -1;
    }
    std::memset(g_InputBuffer, 0, sizeof(g_InputBuffer));
    const int read = hid_read_timeout(g_device, g_InputBuffer, USB_MIN_PACKET_SIZE + 1, 2000);
    if (read < 0) {
        set_hid_error(L"HID read");
        close_device();
    } else if (read == 0) {
        g_lastError = L"HID read timed out after 2000 ms while waiting for the projector reply.";
    }
    return read;
}

int DLPC350_USB_Close() {
    close_device();
    return 0;
}

const std::wstring& DLPC350_USB_LastError() {
    return g_lastError;
}
