#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>

#include <atomic>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr wchar_t kClassName[] = L"CheckerboardCalibrationCapturePanel";
constexpr UINT WM_APP_LOG = WM_APP + 1;
constexpr UINT WM_APP_DONE = WM_APP + 2;

enum ControlId {
    IDC_STATUS = 100,
    IDC_SESSION,
    IDC_POSE,
    IDC_PREPARE,
    IDC_CAPTURE_VISIBLE,
    IDC_CAPTURE_UV,
    IDC_STOP,
    IDC_OPEN,
    IDC_LOG,
};

struct AppState {
    HINSTANCE instance{};
    HWND window{};
    HWND status{};
    HWND session{};
    HWND pose{};
    HWND prepare{};
    HWND captureVisible{};
    HWND captureUv{};
    HWND stop{};
    HWND open{};
    HWND log{};
    HANDLE process = nullptr;
    HANDLE job = nullptr;
    std::atomic_bool running{false};
    std::wstring root;
};

AppState g_app;

std::wstring get_text(HWND hwnd) {
    int length = GetWindowTextLengthW(hwnd);
    std::wstring value(static_cast<size_t>(length) + 1, L'\0');
    GetWindowTextW(hwnd, value.data(), length + 1);
    value.resize(static_cast<size_t>(length));
    return value;
}

std::wstring quote(const std::wstring& value) {
    std::wstring result = L"\"";
    for (wchar_t ch : value) {
        if (ch == L'\"') result += L'\\';
        result += ch;
    }
    result += L"\"";
    return result;
}

std::wstring path_join(const std::wstring& left, const std::wstring& right) {
    if (left.empty()) return right;
    if (left.back() == L'\\' || left.back() == L'/') return left + right;
    return left + L"\\" + right;
}

std::wstring executable_directory() {
    std::vector<wchar_t> buffer(MAX_PATH);
    DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    while (length >= buffer.size() - 1) {
        buffer.resize(buffer.size() * 2);
        length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    }
    std::wstring path(buffer.data(), length);
    const size_t slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? L"." : path.substr(0, slash);
}

bool exists(const std::wstring& path) {
    return GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES;
}

void append_log(const std::wstring& text) {
    const int current = GetWindowTextLengthW(g_app.log);
    SendMessageW(g_app.log, EM_SETSEL, current, current);
    SendMessageW(g_app.log, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(text.c_str()));
    SendMessageW(g_app.log, EM_SCROLLCARET, 0, 0);
}

void set_running(bool running, const wchar_t* status) {
    g_app.running = running;
    SetWindowTextW(g_app.status, status);
    EnableWindow(g_app.prepare, !running);
    EnableWindow(g_app.captureVisible, !running);
    EnableWindow(g_app.captureUv, !running);
    EnableWindow(g_app.open, !running);
    EnableWindow(g_app.session, !running);
    EnableWindow(g_app.pose, !running);
    EnableWindow(g_app.stop, running);
}

HWND make_label(HWND parent, const wchar_t* text, int x, int y, int width, int height) {
    return CreateWindowExW(0, L"STATIC", text, WS_CHILD | WS_VISIBLE, x, y, width, height,
        parent, nullptr, g_app.instance, nullptr);
}

HWND make_edit(HWND parent, int id, const std::wstring& text, int x, int y, int width, int height) {
    return CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", text.c_str(), WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_AUTOHSCROLL,
        x, y, width, height, parent, reinterpret_cast<HMENU>(id), g_app.instance, nullptr);
}

HWND make_button(HWND parent, int id, const wchar_t* text, int x, int y, int width, int height) {
    return CreateWindowExW(0, L"BUTTON", text, WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        x, y, width, height, parent, reinterpret_cast<HMENU>(id), g_app.instance, nullptr);
}

std::wstring pose_id() {
    std::wstring value = get_text(g_app.pose);
    const size_t separator = value.find(L" - ");
    if (separator != std::wstring::npos) value.erase(separator);
    return value;
}

void post_log(std::wstring value) {
    auto* text = new std::wstring(std::move(value));
    PostMessageW(g_app.window, WM_APP_LOG, 0, reinterpret_cast<LPARAM>(text));
}

void start_command(int mode) {
    if (g_app.running) return;
    const std::wstring python = path_join(g_app.root, L".venv-pc\\Scripts\\python.exe");
    const std::wstring script = path_join(g_app.root, L"checkerboard_calibration_capture.py");
    const std::wstring config = path_join(g_app.root, L"camera_config.json");
    const std::wstring patterns = path_join(g_app.root, L"generated_patterns_centered");
    const std::wstring controller = path_join(g_app.root, L"structured_light_pc_controller.py");
    const std::wstring session = get_text(g_app.session);
    if (!exists(python) || !exists(script) || !exists(config)) {
        MessageBoxW(g_app.window, L"Required checkerboard capture files or Python runtime were not found.",
            L"Checkerboard capture", MB_ICONERROR | MB_OK);
        return;
    }
    if (session.empty()) {
        MessageBoxW(g_app.window, L"Enter a calibration session folder.", L"Checkerboard capture", MB_ICONWARNING | MB_OK);
        return;
    }
    std::wstringstream command;
    command << quote(python) << L" -u " << quote(script);
    if (mode != 0) {
        const std::wstring id = pose_id();
        if (id.empty()) {
            MessageBoxW(g_app.window, L"Select or enter a pose ID.", L"Checkerboard capture", MB_ICONWARNING | MB_OK);
            return;
        }
        const wchar_t* subcommand = mode == 1 ? L"capture-visible" : L"capture-uv";
        const wchar_t* prompt = mode == 1
            ? L"Before continuing:\n\n1. Keep the checkerboard fixed.\n2. Turn the UV projector Blue LED OFF.\n3. Turn ON external visible white light.\n\nThis captures and verifies the 9x9 checkerboard reference image."
            : L"Before continuing:\n\n1. Do not move the checkerboard.\n2. Turn OFF external visible white light.\n3. Restore the UV projector Blue LED.\n\nThis captures the UV X and Y pattern sequences.";
        if (MessageBoxW(g_app.window, prompt, L"Checkerboard capture lighting", MB_OKCANCEL | MB_ICONINFORMATION) != IDOK) return;
        command << L" " << subcommand << L" --session " << quote(session)
                << L" --pose-id " << quote(id)
                << L" --controller " << quote(controller);
    } else {
        command << L" setup --session " << quote(session)
                << L" --patterns " << quote(patterns)
                << L" --camera-config " << quote(config);
    }

    SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    HANDLE read_pipe = nullptr;
    HANDLE write_pipe = nullptr;
    if (!CreatePipe(&read_pipe, &write_pipe, &security, 0)) {
        MessageBoxW(g_app.window, L"Could not create the process log pipe.", L"Checkerboard capture", MB_ICONERROR | MB_OK);
        return;
    }
    SetHandleInformation(read_pipe, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdOutput = write_pipe;
    startup.hStdError = write_pipe;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    PROCESS_INFORMATION process{};
    std::wstring mutable_command = command.str();
    const BOOL created = CreateProcessW(nullptr, mutable_command.data(), nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW, nullptr, g_app.root.c_str(), &startup, &process);
    CloseHandle(write_pipe);
    if (!created) {
        CloseHandle(read_pipe);
        MessageBoxW(g_app.window, L"Could not start the checkerboard capture process.", L"Checkerboard capture", MB_ICONERROR | MB_OK);
        return;
    }

    g_app.job = CreateJobObjectW(nullptr, nullptr);
    if (g_app.job != nullptr) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(g_app.job, JobObjectExtendedLimitInformation, &limits, sizeof(limits));
        AssignProcessToJobObject(g_app.job, process.hProcess);
    }
    g_app.process = process.hProcess;
    CloseHandle(process.hThread);
    append_log(L"\r\n> " + command.str() + L"\r\n");
    set_running(true, mode == 1 ? L"Capturing visible checkerboard reference" : mode == 2 ? L"Capturing UV X then Y" : L"Preparing calibration session");
    std::thread([read_pipe, process_handle = process.hProcess]() {
        char buffer[1024];
        DWORD count = 0;
        while (ReadFile(read_pipe, buffer, sizeof(buffer) - 1, &count, nullptr) && count > 0) {
            buffer[count] = '\0';
            int needed = MultiByteToWideChar(CP_UTF8, 0, buffer, -1, nullptr, 0);
            std::wstring line;
            if (needed > 0) {
                line.resize(static_cast<size_t>(needed));
                MultiByteToWideChar(CP_UTF8, 0, buffer, -1, line.data(), needed);
                line.resize(static_cast<size_t>(needed - 1));
            } else {
                line.assign(buffer, buffer + count);
            }
            post_log(std::move(line));
        }
        CloseHandle(read_pipe);
        WaitForSingleObject(process_handle, INFINITE);
        DWORD exit_code = 1;
        GetExitCodeProcess(process_handle, &exit_code);
        CloseHandle(process_handle);
        PostMessageW(g_app.window, WM_APP_DONE, static_cast<WPARAM>(exit_code), 0);
    }).detach();
}

void open_session() {
    const std::wstring session = get_text(g_app.session);
    if (session.empty()) return;
    ShellExecuteW(g_app.window, L"open", session.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void stop_capture() {
    if (!g_app.running || g_app.job == nullptr) return;
    TerminateJobObject(g_app.job, 1);
    append_log(L"\r\n[checkerboard] stop requested\r\n");
}

LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM w_param, LPARAM l_param) {
    switch (message) {
    case WM_COMMAND:
        switch (LOWORD(w_param)) {
        case IDC_PREPARE: start_command(0); return 0;
        case IDC_CAPTURE_VISIBLE: start_command(1); return 0;
        case IDC_CAPTURE_UV: start_command(2); return 0;
        case IDC_STOP: stop_capture(); return 0;
        case IDC_OPEN: open_session(); return 0;
        default: break;
        }
        break;
    case WM_APP_LOG: {
        auto* text = reinterpret_cast<std::wstring*>(l_param);
        append_log(*text);
        delete text;
        return 0;
    }
    case WM_APP_DONE:
        if (g_app.job != nullptr) {
            CloseHandle(g_app.job);
            g_app.job = nullptr;
        }
        g_app.process = nullptr;
        set_running(false, w_param == 0 ? L"Finished" : L"Failed - inspect log");
        append_log(w_param == 0 ? L"\r\n[checkerboard] finished\r\n" : L"\r\n[checkerboard] failed\r\n");
        return 0;
    case WM_CLOSE:
        if (g_app.running) {
            MessageBoxW(hwnd, L"Stop the active capture before closing this window.", L"Checkerboard capture", MB_ICONWARNING | MB_OK);
            return 0;
        }
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        break;
    }
    return DefWindowProcW(hwnd, message, w_param, l_param);
}

void populate_pose_combo() {
    const wchar_t* poses[] = {
        L"p01_center_flat - centered and flat on the stage",
        L"p02_center_spacer_5 - centered, flat, 5 mm spacer",
        L"p03_center_spacer_10 - centered, flat, 10 mm spacer",
        L"p04_left_flat - shifted left, flat",
        L"p05_right_flat - shifted right, flat",
        L"p06_top_flat - shifted away from camera, flat",
        L"p07_bottom_flat - shifted toward camera, flat",
        L"p08_pitch_forward - forward tilt using any stable wedge",
        L"p09_pitch_back - backward tilt using any stable wedge",
        L"p10_roll_left - left tilt using any stable wedge",
        L"p11_roll_right - right tilt using any stable wedge",
        L"p12_diagonal_tilt - combined pitch and roll using a stable wedge",
    };
    for (const wchar_t* pose : poses) SendMessageW(g_app.pose, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(pose));
    SendMessageW(g_app.pose, CB_SETCURSEL, 0, 0);
}

} // namespace

int APIENTRY wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    g_app.instance = instance;
    g_app.root = executable_directory();
    INITCOMMONCONTROLSEX common{sizeof(INITCOMMONCONTROLSEX), ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&common);
    WNDCLASSW window_class{};
    window_class.lpfnWndProc = window_proc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursor(nullptr, IDC_ARROW);
    window_class.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_BTNFACE + 1);
    window_class.lpszClassName = kClassName;
    RegisterClassW(&window_class);
    g_app.window = CreateWindowExW(0, kClassName, L"Checkerboard Calibration Capture", WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 860, 640, nullptr, nullptr, instance, nullptr);
    if (g_app.window == nullptr) return 1;

    make_label(g_app.window, L"Status", 14, 16, 55, 22);
    g_app.status = make_edit(g_app.window, IDC_STATUS, L"Ready", 75, 13, 750, 24);
    SendMessageW(g_app.status, EM_SETREADONLY, TRUE, 0);
    make_label(g_app.window, L"Session", 14, 54, 55, 22);
    g_app.session = make_edit(g_app.window, IDC_SESSION, path_join(g_app.root, L"captures\\checkerboard_calibration_session"), 75, 51, 750, 24);
    make_label(g_app.window, L"Fixed scan", 14, 91, 60, 22);
    make_label(g_app.window, L"15,000 us / 0 dB - 22 X frames + 22 Y frames per pose", 75, 91, 500, 22);
    make_label(g_app.window, L"Pose", 14, 128, 55, 22);
    g_app.pose = CreateWindowExW(0, WC_COMBOBOXW, L"", WS_CHILD | WS_VISIBLE | WS_TABSTOP | CBS_DROPDOWN | WS_VSCROLL,
        75, 125, 475, 260, g_app.window, reinterpret_cast<HMENU>(IDC_POSE), instance, nullptr);
    populate_pose_combo();
    g_app.prepare = make_button(g_app.window, IDC_PREPARE, L"1. Prepare Session", 575, 123, 120, 30);
    g_app.captureVisible = make_button(g_app.window, IDC_CAPTURE_VISIBLE, L"2. Visible Ref", 705, 123, 120, 30);
    g_app.captureUv = make_button(g_app.window, IDC_CAPTURE_UV, L"3. UV X/Y", 575, 160, 120, 30);
    make_label(g_app.window, L"Visible ref: UV off + white light on. UV X/Y: white light off + UV on. Never move the board between steps.", 75, 198, 740, 22);
    g_app.stop = make_button(g_app.window, IDC_STOP, L"Stop", 75, 230, 100, 30);
    g_app.open = make_button(g_app.window, IDC_OPEN, L"Open Session Folder", 185, 230, 145, 30);
    EnableWindow(g_app.stop, FALSE);
    make_label(g_app.window, L"Capture log", 14, 279, 90, 22);
    g_app.log = CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"",
        WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_HSCROLL | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | ES_AUTOHSCROLL,
        14, 302, 811, 288, g_app.window, reinterpret_cast<HMENU>(IDC_LOG), instance, nullptr);
    ShowWindow(g_app.window, show);
    UpdateWindow(g_app.window);
    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return static_cast<int>(message.wParam);
}
