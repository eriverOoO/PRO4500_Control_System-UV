#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <commctrl.h>
#include <shlobj.h>
#include <shellapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "GUI/dlpc350_common.h"
#include "GUI/dlpc350_api.h"
#include "GUI/dlpc350_usb.h"
#include "projector_usb_diagnostics.h"

namespace {

constexpr wchar_t kAppClass[] = L"StructuredLightControlPanelWindow";
constexpr UINT WM_APP_LOG = WM_APP + 1;
constexpr UINT WM_APP_DONE = WM_APP + 2;

enum ControlId {
    IDC_STATUS = 100,
    IDC_PATTERNS,
    IDC_OUTPUT,
    IDC_CAMERA_CONFIG,
    IDC_PROVIDER,
    IDC_DEVICE_INDEX,
    IDC_MONITOR,
    IDC_SETTLE,
    IDC_EXPOSURE,
    IDC_GAIN,
    IDC_FPS,
    IDC_TRIGGER,
    IDC_IMAGE_FORMAT,
    IDC_ANGLES,
    IDC_WINDOWED,
    IDC_STRETCH,
    IDC_PAUSE_FIRST,
    IDC_LOG,
    IDC_START,
    IDC_STOP,
    IDC_PREVIEW,
    IDC_SINGLE_CAPTURE,
    IDC_CONTINUOUS_CAPTURE,
    IDC_NEXT_ANGLE,
    IDC_OPEN_OUTPUT,
    IDC_BROWSE_PATTERNS,
    IDC_BROWSE_OUTPUT,
    IDC_LED_SLIDER,
    IDC_LED_VALUE,
    IDC_APPLY_LED,
    IDC_LED_OFF,
};

enum class JobMode {
    Scan,
    Preview,
    SingleCapture,
    ContinuousCapture,
};

struct AppState {
    HINSTANCE instance{};
    HWND window{};
    HWND status{};
    HWND patterns{};
    HWND output{};
    HWND cameraConfig{};
    HWND provider{};
    HWND deviceIndex{};
    HWND monitor{};
    HWND settle{};
    HWND exposure{};
    HWND gain{};
    HWND fps{};
    HWND trigger{};
    HWND imageFormat{};
    HWND angles{};
    HWND windowed{};
    HWND stretch{};
    HWND pauseFirst{};
    HWND log{};
    HWND start{};
    HWND stop{};
    HWND preview{};
    HWND singleCapture{};
    HWND continuousCapture{};
    HWND nextAngle{};
    HWND ledSlider{};
    HWND ledValue{};
    HWND applyLed{};
    HWND ledOff{};
    PROCESS_INFORMATION jobProcess{};
    HANDLE jobPipeRead = nullptr;
    std::atomic_bool jobRunning{false};
    std::wstring root;
    std::wstring angleAdvanceFile;
    std::wstring jobLabel;
};

AppState g_app;
std::mutex g_usbMutex;

std::wstring quote(const std::wstring& value) {
    std::wstring out = L"\"";
    for (wchar_t ch : value) {
        if (ch == L'"') out += L'\\';
        out += ch;
    }
    out += L"\"";
    return out;
}

std::wstring get_exe_dir() {
    std::vector<wchar_t> buffer(MAX_PATH);
    DWORD len = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    while (len == buffer.size()) {
        buffer.resize(buffer.size() * 2);
        len = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    }
    std::wstring path(buffer.data(), len);
    size_t slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? L"." : path.substr(0, slash);
}

std::wstring path_join(const std::wstring& a, const std::wstring& b) {
    if (a.empty()) return b;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
}

std::wstring runtime_dir() {
    return path_join(g_app.root, L".runtime");
}

std::wstring angle_advance_file() {
    return path_join(runtime_dir(), L"angle_advance.signal");
}

std::wstring get_text(HWND hwnd) {
    int len = GetWindowTextLengthW(hwnd);
    std::wstring value(static_cast<size_t>(len), L'\0');
    GetWindowTextW(hwnd, value.data(), len + 1);
    return value;
}

void set_text(HWND hwnd, const std::wstring& value) {
    SetWindowTextW(hwnd, value.c_str());
}

std::wstring utf8_to_wide(const char* data, int len) {
    if (len <= 0) return L"";
    int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, data, len, nullptr, 0);
    UINT cp = CP_UTF8;
    DWORD flags = MB_ERR_INVALID_CHARS;
    if (size <= 0) {
        cp = CP_ACP;
        flags = 0;
        size = MultiByteToWideChar(cp, flags, data, len, nullptr, 0);
    }
    if (size <= 0) return L"";
    std::wstring out(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(cp, flags, data, len, out.data(), size);
    return out;
}

void post_log(const std::wstring& text) {
    PostMessageW(g_app.window, WM_APP_LOG, 0, reinterpret_cast<LPARAM>(new std::wstring(text)));
}

void append_log(HWND edit, const std::wstring& text) {
    int len = GetWindowTextLengthW(edit);
    SendMessageW(edit, EM_SETSEL, len, len);
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(text.c_str()));
    SendMessageW(edit, EM_SCROLLCARET, 0, 0);
}

void set_status(const std::wstring& value) {
    set_text(g_app.status, value);
}

bool connect_projector(std::wstring& error) {
    if (DLPC350_USB_Init() != 0) {
        error = L"HIDAPI init failed";
        return false;
    }
    if (DLPC350_USB_Open() != 0) {
        error = DLPC350_USB_LastError();
        DLPC350_USB_Exit();
        if (error.empty()) {
            error = L"LightCrafter 4500 not found or cannot be opened (VID 0451, PID 6401)";
        }
        return false;
    }
    return true;
}

void disconnect_projector() {
    DLPC350_USB_Close();
    DLPC350_USB_Exit();
}

bool set_blue_led(int brightness, std::wstring& error) {
    std::lock_guard<std::mutex> lock(g_usbMutex);
    if (!connect_projector(error)) {
        return false;
    }

    const unsigned char current = static_cast<unsigned char>(255 - std::clamp(brightness, 0, 255));
    const int enableResult = DLPC350_SetLedEnables(false, false, false, brightness > 0);
    const int currentResult = DLPC350_SetLedCurrents(255, 255, current);
    disconnect_projector();

    if (enableResult < 0 || currentResult < 0) {
        error = L"Blue LED command failed";
        return false;
    }
    return true;
}

void update_led_value_label() {
    const int value = static_cast<int>(SendMessageW(g_app.ledSlider, TBM_GETPOS, 0, 0));
    set_text(g_app.ledValue, std::to_wstring(value));
}

void apply_led_value(int value) {
    SendMessageW(g_app.ledSlider, TBM_SETPOS, TRUE, value);
    update_led_value_label();

    std::wstring error;
    if (set_blue_led(value, error)) {
        set_status(value > 0 ? L"Blue LED applied" : L"Blue LED off");
        append_log(g_app.log, L"\r\n[led] Blue LED brightness " + std::to_wstring(value) + L" applied.\r\n");
    } else {
        set_status(L"LED Error");
        append_log(g_app.log, L"\r\n[led] " + error + L"\r\n");
    }
}

void handle_job_log(const std::wstring& text) {
    append_log(g_app.log, text);
    if (text.find(L"[angle] Waiting") != std::wstring::npos) {
        EnableWindow(g_app.nextAngle, TRUE);
        set_status(L"Waiting Angle");
    }
    if (text.find(L"[angle] Continue") != std::wstring::npos) {
        EnableWindow(g_app.nextAngle, FALSE);
        set_status(L"Running");
    }
    if (text.find(L"[camera] opened") != std::wstring::npos) {
        set_status(L"Camera Ready");
    }
    if (text.find(L"[camera] ERROR") != std::wstring::npos || text.find(L"[scan] ERROR") != std::wstring::npos) {
        set_status(L"Failed");
    }
    if (text.find(L"[capture] saved") != std::wstring::npos) {
        set_status(L"Capturing");
    }
}

HWND make_label(HWND parent, const wchar_t* text, int x, int y, int w, int h) {
    HWND hwnd = CreateWindowW(L"STATIC", text, WS_CHILD | WS_VISIBLE, x, y, w, h, parent, nullptr, g_app.instance, nullptr);
    SendMessageW(hwnd, WM_SETFONT, reinterpret_cast<WPARAM>(GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    return hwnd;
}

HWND make_edit(HWND parent, int id, const std::wstring& text, int x, int y, int w, int h) {
    HWND hwnd = CreateWindowExW(
        WS_EX_CLIENTEDGE, L"EDIT", text.c_str(),
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_AUTOHSCROLL,
        x, y, w, h, parent, reinterpret_cast<HMENU>(id), g_app.instance, nullptr);
    SendMessageW(hwnd, WM_SETFONT, reinterpret_cast<WPARAM>(GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    return hwnd;
}

HWND make_button(HWND parent, int id, const wchar_t* text, int x, int y, int w, int h) {
    HWND hwnd = CreateWindowW(
        L"BUTTON", text, WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        x, y, w, h, parent, reinterpret_cast<HMENU>(id), g_app.instance, nullptr);
    SendMessageW(hwnd, WM_SETFONT, reinterpret_cast<WPARAM>(GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    return hwnd;
}

HWND make_checkbox(HWND parent, int id, const wchar_t* text, int x, int y, int w, int h, bool checked) {
    HWND hwnd = CreateWindowW(
        L"BUTTON", text, WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_AUTOCHECKBOX,
        x, y, w, h, parent, reinterpret_cast<HMENU>(id), g_app.instance, nullptr);
    SendMessageW(hwnd, WM_SETFONT, reinterpret_cast<WPARAM>(GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    SendMessageW(hwnd, BM_SETCHECK, checked ? BST_CHECKED : BST_UNCHECKED, 0);
    return hwnd;
}

std::wstring browse_folder(HWND owner, const wchar_t* title, const std::wstring& current) {
    BROWSEINFOW bi{};
    bi.hwndOwner = owner;
    bi.lpszTitle = title;
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
    PIDLIST_ABSOLUTE pidl = SHBrowseForFolderW(&bi);
    if (!pidl) return current;
    wchar_t path[MAX_PATH]{};
    std::wstring result = current;
    if (SHGetPathFromIDListW(pidl, path)) result = path;
    CoTaskMemFree(pidl);
    return result;
}

bool file_exists(const std::wstring& path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool dir_exists(const std::wstring& path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY);
}

long long current_epoch_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

bool write_text_file(const std::wstring& path, const std::string& text) {
    HANDLE file = CreateFileW(
        path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    DWORD written = 0;
    BOOL ok = WriteFile(file, text.data(), static_cast<DWORD>(text.size()), &written, nullptr);
    CloseHandle(file);
    return ok && written == text.size();
}

void signal_next_angle() {
    if (!g_app.jobRunning.load()) return;
    if (g_app.angleAdvanceFile.empty()) {
        g_app.angleAdvanceFile = angle_advance_file();
    }
    CreateDirectoryW(runtime_dir().c_str(), nullptr);
    std::string token = std::to_string(current_epoch_ms());
    if (write_text_file(g_app.angleAdvanceFile, token)) {
        append_log(g_app.log, L"\r\n[ui] Next Angle signal sent. Continue after rotation.\r\n");
        EnableWindow(g_app.nextAngle, FALSE);
        set_status(L"Running");
    } else {
        append_log(g_app.log, L"\r\n[ui] Failed to send Next Angle signal.\r\n");
    }
}

std::wstring find_python() {
    std::wstring venvPython = path_join(g_app.root, L".venv-pc\\Scripts\\python.exe");
    if (file_exists(venvPython)) return venvPython;
    return L"python.exe";
}

void append_optional_arg(std::wstringstream& cmd, const wchar_t* name, HWND hwnd) {
    std::wstring value = get_text(hwnd);
    if (!value.empty()) {
        cmd << L" " << name << L" " << quote(value);
    }
}

std::wstring build_controller_command(JobMode mode) {
    std::wstring python = find_python();
    std::wstring controller = path_join(g_app.root, L"structured_light_pc_controller.py");

    std::wstringstream cmd;
    cmd << quote(python) << L" -u " << quote(controller)
        << L" --patterns " << quote(get_text(g_app.patterns))
        << L" --output " << quote(get_text(g_app.output))
        << L" --camera-config " << quote(get_text(g_app.cameraConfig))
        << L" --monitor " << quote(get_text(g_app.monitor))
        << L" --settle-ms " << quote(get_text(g_app.settle))
        << L" --angles " << quote(get_text(g_app.angles))
        << L" --angle-advance-file " << quote(g_app.angleAdvanceFile)
        << L" --save-format " << quote(L"png");

    append_optional_arg(cmd, L"--camera-provider", g_app.provider);
    append_optional_arg(cmd, L"--camera-device-index", g_app.deviceIndex);
    append_optional_arg(cmd, L"--exposure-us", g_app.exposure);
    append_optional_arg(cmd, L"--gain-db", g_app.gain);
    append_optional_arg(cmd, L"--fps", g_app.fps);
    append_optional_arg(cmd, L"--trigger-mode", g_app.trigger);
    append_optional_arg(cmd, L"--image-format", g_app.imageFormat);

    if (SendMessageW(g_app.windowed, BM_GETCHECK, 0, 0) == BST_CHECKED) cmd << L" --windowed";
    if (SendMessageW(g_app.stretch, BM_GETCHECK, 0, 0) == BST_CHECKED) cmd << L" --stretch";
    if (SendMessageW(g_app.pauseFirst, BM_GETCHECK, 0, 0) == BST_CHECKED) cmd << L" --pause-before-first-angle";

    if (mode == JobMode::Preview) {
        cmd << L" --preview";
    } else if (mode == JobMode::SingleCapture) {
        cmd << L" --single-capture";
    } else if (mode == JobMode::ContinuousCapture) {
        cmd << L" --continuous-capture " << quote(L"0");
    }

    return cmd.str();
}

void set_job_buttons(bool running) {
    EnableWindow(g_app.start, !running);
    EnableWindow(g_app.preview, !running);
    EnableWindow(g_app.singleCapture, !running);
    EnableWindow(g_app.continuousCapture, !running);
    EnableWindow(g_app.stop, running);
    if (!running) EnableWindow(g_app.nextAngle, FALSE);
}

void read_pipe_thread(HANDLE pipe) {
    char buffer[4096];
    DWORD read = 0;
    while (ReadFile(pipe, buffer, sizeof(buffer), &read, nullptr) && read > 0) {
        post_log(utf8_to_wide(buffer, static_cast<int>(read)));
    }
    CloseHandle(pipe);
}

void wait_process_thread(HANDLE process) {
    WaitForSingleObject(process, INFINITE);
    DWORD exitCode = 0;
    GetExitCodeProcess(process, &exitCode);
    PostMessageW(g_app.window, WM_APP_DONE, exitCode, 0);
}

void start_job(JobMode mode, const std::wstring& label) {
    if (g_app.jobRunning.load()) return;

    std::wstring controller = path_join(g_app.root, L"structured_light_pc_controller.py");
    if (!file_exists(controller)) {
        MessageBoxW(g_app.window, L"structured_light_pc_controller.py was not found.", L"Missing Controller", MB_ICONERROR);
        return;
    }
    if (mode == JobMode::Scan && !dir_exists(get_text(g_app.patterns))) {
        MessageBoxW(g_app.window, L"Pattern folder does not exist.", L"Missing Patterns", MB_ICONERROR);
        return;
    }

    CreateDirectoryW(runtime_dir().c_str(), nullptr);
    g_app.angleAdvanceFile = angle_advance_file();
    DeleteFileW(g_app.angleAdvanceFile.c_str());
    g_app.jobLabel = label;
    append_log(g_app.log, L"\r\n=== Started " + label + L" ===\r\n");

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE readPipe = nullptr;
    HANDLE writePipe = nullptr;
    if (!CreatePipe(&readPipe, &writePipe, &sa, 0)) {
        MessageBoxW(g_app.window, L"Failed to create output pipe.", L"Error", MB_ICONERROR);
        return;
    }
    SetHandleInformation(readPipe, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = writePipe;
    si.hStdError = writePipe;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    ZeroMemory(&g_app.jobProcess, sizeof(g_app.jobProcess));
    std::wstring cmd = build_controller_command(mode);
    std::vector<wchar_t> mutableCmd(cmd.begin(), cmd.end());
    mutableCmd.push_back(L'\0');

    BOOL ok = CreateProcessW(
        nullptr, mutableCmd.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
        nullptr, g_app.root.c_str(), &si, &g_app.jobProcess);
    CloseHandle(writePipe);

    if (!ok) {
        CloseHandle(readPipe);
        MessageBoxW(g_app.window, L"Failed to start Python controller. Run prepare_pc_python_env.ps1 or install Python on PATH.", L"Error", MB_ICONERROR);
        return;
    }

    g_app.jobPipeRead = readPipe;
    g_app.jobRunning.store(true);
    set_job_buttons(true);
    set_status(L"Running");
    std::thread(read_pipe_thread, readPipe).detach();
    std::thread(wait_process_thread, g_app.jobProcess.hProcess).detach();
}

void stop_job() {
    if (!g_app.jobRunning.load()) return;
    if (MessageBoxW(g_app.window, L"Stop the running job?", L"Stop", MB_YESNO | MB_ICONQUESTION) != IDYES) return;
    TerminateProcess(g_app.jobProcess.hProcess, 130);
    EnableWindow(g_app.nextAngle, FALSE);
    set_status(L"Stopping");
}

void open_output() {
    std::wstring output = get_text(g_app.output);
    CreateDirectoryW(output.c_str(), nullptr);
    ShellExecuteW(g_app.window, L"open", output.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void build_ui(HWND hwnd) {
    int margin = 14;
    int y = 12;
    make_label(hwnd, L"Status", margin, y + 4, 50, 22);
    g_app.status = make_label(hwnd, L"Idle", margin + 56, y + 4, 190, 22);
    make_label(hwnd, L"Config", 315, y + 4, 50, 22);
    g_app.cameraConfig = make_edit(hwnd, IDC_CAMERA_CONFIG, path_join(g_app.root, L"camera_config.json"), 365, y, 560, 24);

    y += 36;
    make_label(hwnd, L"Blue LED", margin, y + 4, 70, 22);
    g_app.ledSlider = CreateWindowW(
        TRACKBAR_CLASSW, L"",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | TBS_HORZ | TBS_AUTOTICKS,
        90, y, 260, 32, hwnd, reinterpret_cast<HMENU>(IDC_LED_SLIDER), g_app.instance, nullptr);
    SendMessageW(g_app.ledSlider, TBM_SETRANGE, TRUE, MAKELPARAM(0, 255));
    SendMessageW(g_app.ledSlider, TBM_SETPOS, TRUE, 128);
    g_app.ledValue = make_label(hwnd, L"128", 360, y + 4, 45, 22);
    g_app.applyLed = make_button(hwnd, IDC_APPLY_LED, L"Apply LED", 425, y, 110, 28);
    g_app.ledOff = make_button(hwnd, IDC_LED_OFF, L"LED Off", 550, y, 90, 28);

    y += 42;
    make_label(hwnd, L"Patterns", margin, y + 4, 80, 22);
    g_app.patterns = make_edit(hwnd, IDC_PATTERNS, path_join(g_app.root, L"generated_patterns"), 110, y, 700, 24);
    make_button(hwnd, IDC_BROWSE_PATTERNS, L"Browse", 825, y, 100, 24);

    y += 32;
    make_label(hwnd, L"Output", margin, y + 4, 80, 22);
    g_app.output = make_edit(hwnd, IDC_OUTPUT, path_join(g_app.root, L"captures"), 110, y, 700, 24);
    make_button(hwnd, IDC_BROWSE_OUTPUT, L"Browse", 825, y, 100, 24);

    y += 38;
    make_label(hwnd, L"Provider", margin, y + 4, 70, 22);
    g_app.provider = make_edit(hwnd, IDC_PROVIDER, L"mock", 85, y, 90, 24);
    make_label(hwnd, L"Device", 198, y + 4, 55, 22);
    g_app.deviceIndex = make_edit(hwnd, IDC_DEVICE_INDEX, L"0", 252, y, 50, 24);
    make_label(hwnd, L"Exposure us", 325, y + 4, 85, 22);
    g_app.exposure = make_edit(hwnd, IDC_EXPOSURE, L"10000", 410, y, 90, 24);
    make_label(hwnd, L"Gain dB", 522, y + 4, 60, 22);
    g_app.gain = make_edit(hwnd, IDC_GAIN, L"0.0", 585, y, 70, 24);
    make_label(hwnd, L"FPS", 680, y + 4, 35, 22);
    g_app.fps = make_edit(hwnd, IDC_FPS, L"15.0", 715, y, 70, 24);
    make_label(hwnd, L"Trigger", 805, y + 4, 60, 22);
    g_app.trigger = make_edit(hwnd, IDC_TRIGGER, L"software", 865, y, 70, 24);

    y += 34;
    make_label(hwnd, L"Format", margin, y + 4, 55, 22);
    g_app.imageFormat = make_edit(hwnd, IDC_IMAGE_FORMAT, L"mono8", 75, y, 80, 24);
    make_label(hwnd, L"Monitor", 185, y + 4, 60, 22);
    g_app.monitor = make_edit(hwnd, IDC_MONITOR, L"1", 250, y, 60, 24);
    make_label(hwnd, L"Angles", 340, y + 4, 55, 22);
    g_app.angles = make_edit(hwnd, IDC_ANGLES, L"0", 395, y, 145, 24);
    make_label(hwnd, L"Settle ms", 575, y + 4, 75, 22);
    g_app.settle = make_edit(hwnd, IDC_SETTLE, L"300", 655, y, 80, 24);

    y += 34;
    g_app.windowed = make_checkbox(hwnd, IDC_WINDOWED, L"Windowed projection", margin, y, 170, 24, false);
    g_app.stretch = make_checkbox(hwnd, IDC_STRETCH, L"Stretch patterns", 205, y, 140, 24, false);
    g_app.pauseFirst = make_checkbox(hwnd, IDC_PAUSE_FIRST, L"Pause before first angle", 370, y, 190, 24, false);

    y += 42;
    g_app.start = make_button(hwnd, IDC_START, L"Start Scan", margin, y, 115, 32);
    g_app.preview = make_button(hwnd, IDC_PREVIEW, L"Preview", 142, y, 95, 32);
    g_app.singleCapture = make_button(hwnd, IDC_SINGLE_CAPTURE, L"Single Capture", 250, y, 120, 32);
    g_app.continuousCapture = make_button(hwnd, IDC_CONTINUOUS_CAPTURE, L"Continuous", 385, y, 115, 32);
    g_app.stop = make_button(hwnd, IDC_STOP, L"Stop", 515, y, 80, 32);
    EnableWindow(g_app.stop, FALSE);
    g_app.nextAngle = make_button(hwnd, IDC_NEXT_ANGLE, L"Next Angle", 610, y, 115, 32);
    EnableWindow(g_app.nextAngle, FALSE);
    make_button(hwnd, IDC_OPEN_OUTPUT, L"Open Output Folder", 740, y, 160, 32);

    y += 48;
    g_app.log = CreateWindowExW(
        WS_EX_CLIENTEDGE, L"EDIT", L"",
        WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_HSCROLL | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | ES_AUTOHSCROLL,
        margin, y, 912, 390, hwnd, reinterpret_cast<HMENU>(IDC_LOG), g_app.instance, nullptr);
    SendMessageW(g_app.log, WM_SETFONT, reinterpret_cast<WPARAM>(GetStockObject(DEFAULT_GUI_FONT)), TRUE);
}

LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    switch (msg) {
    case WM_CREATE:
        g_app.window = hwnd;
        build_ui(hwnd);
        return 0;
    case WM_COMMAND: {
        int id = LOWORD(wparam);
        switch (id) {
        case IDC_BROWSE_PATTERNS:
            set_text(g_app.patterns, browse_folder(hwnd, L"Select pattern folder", get_text(g_app.patterns)));
            return 0;
        case IDC_BROWSE_OUTPUT:
            set_text(g_app.output, browse_folder(hwnd, L"Select output folder", get_text(g_app.output)));
            return 0;
        case IDC_START:
            start_job(JobMode::Scan, L"scan");
            return 0;
        case IDC_PREVIEW:
            start_job(JobMode::Preview, L"preview");
            return 0;
        case IDC_SINGLE_CAPTURE:
            start_job(JobMode::SingleCapture, L"single capture");
            return 0;
        case IDC_CONTINUOUS_CAPTURE:
            start_job(JobMode::ContinuousCapture, L"continuous capture");
            return 0;
        case IDC_STOP:
            stop_job();
            return 0;
        case IDC_NEXT_ANGLE:
            signal_next_angle();
            return 0;
        case IDC_APPLY_LED:
            apply_led_value(static_cast<int>(SendMessageW(g_app.ledSlider, TBM_GETPOS, 0, 0)));
            return 0;
        case IDC_LED_OFF:
            apply_led_value(0);
            return 0;
        case IDC_OPEN_OUTPUT:
            open_output();
            return 0;
        default:
            break;
        }
        break;
    }
    case WM_HSCROLL:
        if (reinterpret_cast<HWND>(lparam) == g_app.ledSlider) {
            update_led_value_label();
            return 0;
        }
        break;
    case WM_APP_LOG: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        if (text) {
            handle_job_log(*text);
            delete text;
        }
        return 0;
    }
    case WM_APP_DONE: {
        DWORD exitCode = static_cast<DWORD>(wparam);
        if (g_app.jobProcess.hThread) CloseHandle(g_app.jobProcess.hThread);
        if (g_app.jobProcess.hProcess) CloseHandle(g_app.jobProcess.hProcess);
        ZeroMemory(&g_app.jobProcess, sizeof(g_app.jobProcess));
        g_app.jobRunning.store(false);
        set_job_buttons(false);
        std::wstringstream ss;
        ss << L"\r\n=== " << g_app.jobLabel << L" finished with exit code " << exitCode << L" ===\r\n";
        append_log(g_app.log, ss.str());
        set_status(exitCode == 0 ? L"Finished" : L"Failed");
        return 0;
    }
    case WM_CLOSE:
        if (g_app.jobRunning.load()) {
            if (MessageBoxW(hwnd, L"A job is running. Stop it and exit?", L"Exit", MB_YESNO | MB_ICONQUESTION) != IDYES) return 0;
            TerminateProcess(g_app.jobProcess.hProcess, 130);
        }
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        break;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    g_app.instance = instance;
    g_app.root = get_exe_dir();

    INITCOMMONCONTROLSEX icc{};
    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_STANDARD_CLASSES | ICC_BAR_CLASSES;
    InitCommonControlsEx(&icc);
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    WNDCLASSW wc{};
    wc.lpfnWndProc = wnd_proc;
    wc.hInstance = instance;
    wc.lpszClassName = kAppClass;
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hIcon = LoadIcon(nullptr, IDI_APPLICATION);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    RegisterClassW(&wc);

    HWND hwnd = CreateWindowExW(
        0, kAppClass, L"PRO4500 XIMEA UV Scan Controller",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 960, 760,
        nullptr, nullptr, instance, nullptr);

    if (!hwnd) return 1;
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);

    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    CoUninitialize();
    return static_cast<int>(msg.wParam);
}
