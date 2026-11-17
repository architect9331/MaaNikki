//! 原生后台鼠标借用拦截守护模块
//!
//! 在后台模式瞬时借用系统光标期间，通过独立原生线程安装 Windows 全局低级鼠标钩子
//! （`WH_MOUSE_LL`），屏蔽物理鼠标移动、点击与滚轮对游戏或前台拖拽的干扰，
//! 同时保持键盘完全畅通，并在归还光标、进程退出或租约超时时立即自动放行并卸载钩子。

#[cfg(windows)]
mod imp {
    use std::ffi::{c_void, OsString};
    use std::os::windows::ffi::OsStrExt;
    use std::ptr;
    use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
    use std::sync::{Mutex, OnceLock};

    #[used]
    pub static CAPABILITY_MARKER: [u8; 31] = *b"MAANIKKI_MOUSE_GUARD_NATIVE_V1\0";

    const SHM_MAGIC: u32 = 0x4D4E_4D47; // "MNMG"
    const SHM_VERSION: u32 = 1;

    const CMD_DISARM: u32 = 0;
    const CMD_ARM: u32 = 1;
    const CMD_RENEW: u32 = 2;
    const CMD_SHUTDOWN: u32 = 3;

    const STATE_DISARMED: u32 = 0;
    const STATE_ARMED: u32 = 1;
    const STATE_DEFERRED_BUTTON_DOWN: u32 = 2;
    const STATE_HOOK_FAILED: u32 = 3;

    const DEFAULT_LEASE_MS: u32 = 450;
    const MIN_LEASE_MS: u32 = 50;
    const MAX_LEASE_MS: u32 = 1000;
    const HARD_SESSION_CAP_MS: u64 = 2500;

    const WH_MOUSE_LL: i32 = 14;
    const WM_QUIT: u32 = 0x0012;
    const WM_USER: u32 = 0x0400;
    const WM_MOUSEMOVE: usize = 0x0200;
    const WM_LBUTTONDOWN: usize = 0x0201;
    const WM_LBUTTONUP: usize = 0x0202;
    const WM_RBUTTONDOWN: usize = 0x0204;
    const WM_RBUTTONUP: usize = 0x0205;
    const WM_MBUTTONDOWN: usize = 0x0207;
    const WM_MBUTTONUP: usize = 0x0208;
    const WM_MOUSEWHEEL: usize = 0x020A;
    const WM_XBUTTONDOWN: usize = 0x020B;
    const WM_XBUTTONUP: usize = 0x020C;
    const WM_MOUSEHWHEEL: usize = 0x020E;

    const VK_LBUTTON: i32 = 0x01;
    const VK_RBUTTON: i32 = 0x02;
    const VK_MBUTTON: i32 = 0x04;
    const VK_XBUTTON1: i32 = 0x05;
    const VK_XBUTTON2: i32 = 0x06;

    const BIT_L: u32 = 1 << 0;
    const BIT_R: u32 = 1 << 1;
    const BIT_M: u32 = 1 << 2;
    const BIT_X1: u32 = 1 << 3;
    const BIT_X2: u32 = 1 << 4;

    const PAGE_READWRITE: u32 = 0x04;
    const FILE_MAP_ALL_ACCESS: u32 = 0x000F_001F;
    const EVENT_MODIFY_STATE: u32 = 0x0002;
    const SYNCHRONIZE: u32 = 0x0010_0000;
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const PM_NOREMOVE: u32 = 0x0000;
    const PM_REMOVE: u32 = 0x0001;
    const QS_ALLINPUT: u32 = 0x04FF;
    const MWMO_INPUTAVAILABLE: u32 = 0x0004;
    const INFINITE: u32 = 0xFFFF_FFFF;
    const WAIT_OBJECT_0: u32 = 0;
    const STILL_ACTIVE: u32 = 259;

    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    struct Msg {
        hwnd: *mut c_void,
        message: u32,
        w_param: usize,
        l_param: isize,
        time: u32,
        pt: Point,
        l_private: u32,
    }

    #[repr(C)]
    struct MsLlHookStruct {
        pt: Point,
        mouse_data: u32,
        flags: u32,
        time: u32,
        dw_extra_info: usize,
    }

    #[repr(C)]
    struct SharedState {
        magic: u32,
        version: u32,
        cmd_seq: AtomicU32,
        ack_seq: AtomicU32,
        command: AtomicU32,
        state: AtomicU32,
        lease_ms: AtomicU32,
        owner_pid: AtomicU32,
        blocked_count: AtomicU32,
        passed_up_count: AtomicU32,
        hook_error: AtomicU32,
        reserved: [u32; 5],
    }

    type HookProc = unsafe extern "system" fn(i32, usize, isize) -> isize;

    #[link(name = "user32")]
    extern "system" {
        fn SetWindowsHookExW(
            id_hook: i32,
            lpfn: Option<HookProc>,
            hmod: *mut c_void,
            dw_thread_id: u32,
        ) -> *mut c_void;
        fn UnhookWindowsHookEx(hhk: *mut c_void) -> i32;
        fn CallNextHookEx(hhk: *mut c_void, n_code: i32, w_param: usize, l_param: isize) -> isize;
        fn PeekMessageW(
            lp_msg: *mut Msg,
            hwnd: *mut c_void,
            w_msg_filter_min: u32,
            w_msg_filter_max: u32,
            w_remove_msg: u32,
        ) -> i32;
        fn TranslateMessage(lp_msg: *const Msg) -> i32;
        fn DispatchMessageW(lp_msg: *const Msg) -> isize;
        fn MsgWaitForMultipleObjectsEx(
            n_count: u32,
            p_handles: *const *mut c_void,
            dw_milliseconds: u32,
            dw_wake_mask: u32,
            dw_flags: u32,
        ) -> u32;
        fn GetAsyncKeyState(v_key: i32) -> i16;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(lp_module_name: *const u16) -> *mut c_void;
        fn GetTickCount64() -> u64;
        fn GetLastError() -> u32;
        fn CreateFileMappingW(
            h_file: *mut c_void,
            lp_file_mapping_attributes: *mut c_void,
            fl_protect: u32,
            dw_maximum_size_high: u32,
            dw_maximum_size_low: u32,
            lp_name: *const u16,
        ) -> *mut c_void;
        fn OpenFileMappingW(
            dw_desired_access: u32,
            b_inherit_handle: i32,
            lp_name: *const u16,
        ) -> *mut c_void;
        fn MapViewOfFile(
            h_file_mapping_object: *mut c_void,
            dw_desired_access: u32,
            dw_file_offset_high: u32,
            dw_file_offset_low: u32,
            dw_number_of_bytes_to_map: usize,
        ) -> *mut c_void;
        fn UnmapViewOfFile(lp_base_address: *const c_void) -> i32;
        fn CreateEventW(
            lp_event_attributes: *mut c_void,
            b_manual_reset: i32,
            b_initial_state: i32,
            lp_name: *const u16,
        ) -> *mut c_void;
        fn OpenEventW(
            dw_desired_access: u32,
            b_inherit_handle: i32,
            lp_name: *const u16,
        ) -> *mut c_void;
        fn SetEvent(h_event: *mut c_void) -> i32;
        fn WaitForSingleObject(h_handle: *mut c_void, dw_milliseconds: u32) -> u32;
        fn OpenProcess(
            dw_desired_access: u32,
            b_inherit_handle: i32,
            dw_process_id: u32,
        ) -> *mut c_void;
        fn GetExitCodeProcess(h_process: *mut c_void, lp_exit_code: *mut u32) -> i32;
        fn CloseHandle(h_object: *mut c_void) -> i32;
    }

    static HOOK_ARMED: AtomicBool = AtomicBool::new(false);
    static HOOK_LEASE_DEADLINE_MS: AtomicU64 = AtomicU64::new(0);
    static HOOK_HARD_DEADLINE_MS: AtomicU64 = AtomicU64::new(0);
    static SWALLOWED_DOWN_MASK: AtomicU32 = AtomicU32::new(0);
    static ACTIVE_SHM_PTR: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

    fn to_wide(s: &str) -> Vec<u16> {
        std::ffi::OsStr::new(s)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    fn any_mouse_button_down() -> bool {
        unsafe {
            for vk in [VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2] {
                if (GetAsyncKeyState(vk) as u16) & 0x8000 != 0 {
                    return true;
                }
            }
        }
        false
    }

    fn record_blocked() {
        let addr = ACTIVE_SHM_PTR.load(Ordering::Relaxed);
        if addr != 0 {
            let shm = unsafe { &*(addr as *const SharedState) };
            shm.blocked_count.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn record_passed_up() {
        let addr = ACTIVE_SHM_PTR.load(Ordering::Relaxed);
        if addr != 0 {
            let shm = unsafe { &*(addr as *const SharedState) };
            shm.passed_up_count.fetch_add(1, Ordering::Relaxed);
        }
    }

    unsafe extern "system" fn low_level_mouse_proc(
        n_code: i32,
        w_param: usize,
        l_param: isize,
    ) -> isize {
        if n_code < 0 || !HOOK_ARMED.load(Ordering::Acquire) {
            return CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param);
        }

        let now = GetTickCount64();
        let lease_deadline = HOOK_LEASE_DEADLINE_MS.load(Ordering::Acquire);
        let hard_deadline = HOOK_HARD_DEADLINE_MS.load(Ordering::Acquire);
        if now >= lease_deadline || now >= hard_deadline {
            // 超时立即自动放行，避免游戏卡顿或异常时拖住系统鼠标。
            HOOK_ARMED.store(false, Ordering::Release);
            return CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param);
        }

        match w_param {
            WM_MOUSEMOVE | WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
                record_blocked();
                1
            }
            WM_LBUTTONDOWN => {
                SWALLOWED_DOWN_MASK.fetch_or(BIT_L, Ordering::AcqRel);
                record_blocked();
                1
            }
            WM_LBUTTONUP => {
                if (SWALLOWED_DOWN_MASK.fetch_and(!BIT_L, Ordering::AcqRel) & BIT_L) != 0 {
                    record_blocked();
                    1
                } else {
                    record_passed_up();
                    CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param)
                }
            }
            WM_RBUTTONDOWN => {
                SWALLOWED_DOWN_MASK.fetch_or(BIT_R, Ordering::AcqRel);
                record_blocked();
                1
            }
            WM_RBUTTONUP => {
                if (SWALLOWED_DOWN_MASK.fetch_and(!BIT_R, Ordering::AcqRel) & BIT_R) != 0 {
                    record_blocked();
                    1
                } else {
                    record_passed_up();
                    CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param)
                }
            }
            WM_MBUTTONDOWN => {
                SWALLOWED_DOWN_MASK.fetch_or(BIT_M, Ordering::AcqRel);
                record_blocked();
                1
            }
            WM_MBUTTONUP => {
                if (SWALLOWED_DOWN_MASK.fetch_and(!BIT_M, Ordering::AcqRel) & BIT_M) != 0 {
                    record_blocked();
                    1
                } else {
                    record_passed_up();
                    CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param)
                }
            }
            WM_XBUTTONDOWN => {
                let bit = if l_param != 0 {
                    let info = &*(l_param as *const MsLlHookStruct);
                    match (info.mouse_data >> 16) & 0xFFFF {
                        1 => BIT_X1,
                        2 => BIT_X2,
                        _ => 0,
                    }
                } else {
                    0
                };
                if bit != 0 {
                    SWALLOWED_DOWN_MASK.fetch_or(bit, Ordering::AcqRel);
                }
                record_blocked();
                1
            }
            WM_XBUTTONUP => {
                let bit = if l_param != 0 {
                    let info = &*(l_param as *const MsLlHookStruct);
                    match (info.mouse_data >> 16) & 0xFFFF {
                        1 => BIT_X1,
                        2 => BIT_X2,
                        _ => 0,
                    }
                } else {
                    0
                };
                if bit != 0 && (SWALLOWED_DOWN_MASK.fetch_and(!bit, Ordering::AcqRel) & bit) != 0 {
                    record_blocked();
                    1
                } else {
                    record_passed_up();
                    CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param)
                }
            }
            _ => CallNextHookEx(ptr::null_mut(), n_code, w_param, l_param),
        }
    }

    struct GuardHandles {
        hook: *mut c_void,
        owner_handle: *mut c_void,
        owner_pid: u32,
        lease_deadline: u64,
        hard_deadline: u64,
    }

    impl GuardHandles {
        fn new() -> Self {
            Self {
                hook: ptr::null_mut(),
                owner_handle: ptr::null_mut(),
                owner_pid: 0,
                lease_deadline: 0,
                hard_deadline: 0,
            }
        }

        unsafe fn disarm(&mut self, shm: &SharedState) {
            HOOK_ARMED.store(false, Ordering::Release);
            HOOK_LEASE_DEADLINE_MS.store(0, Ordering::Release);
            HOOK_HARD_DEADLINE_MS.store(0, Ordering::Release);
            SWALLOWED_DOWN_MASK.store(0, Ordering::Release);
            if !self.hook.is_null() {
                UnhookWindowsHookEx(self.hook);
                self.hook = ptr::null_mut();
            }
            if !self.owner_handle.is_null() {
                CloseHandle(self.owner_handle);
                self.owner_handle = ptr::null_mut();
            }
            self.owner_pid = 0;
            self.lease_deadline = 0;
            self.hard_deadline = 0;
            shm.state.store(STATE_DISARMED, Ordering::Release);
        }

        unsafe fn owner_alive(&self) -> bool {
            if self.owner_handle.is_null() {
                return true;
            }
            if WaitForSingleObject(self.owner_handle, 0) == WAIT_OBJECT_0 {
                return false;
            }
            let mut code: u32 = 0;
            if GetExitCodeProcess(self.owner_handle, &mut code) != 0 {
                return code == STILL_ACTIVE;
            }
            true
        }

        unsafe fn arm(&mut self, shm: &SharedState) {
            let hmod = GetModuleHandleW(ptr::null());
            if self.hook.is_null() {
                HOOK_ARMED.store(false, Ordering::Release);
                let hook = SetWindowsHookExW(WH_MOUSE_LL, Some(low_level_mouse_proc), hmod, 0);
                if hook.is_null() {
                    shm.hook_error.store(GetLastError(), Ordering::Release);
                    shm.state.store(STATE_HOOK_FAILED, Ordering::Release);
                    return;
                }
                self.hook = hook;
            }

            if any_mouse_button_down() {
                self.disarm(shm);
                shm.state
                    .store(STATE_DEFERRED_BUTTON_DOWN, Ordering::Release);
                return;
            }

            let pid = shm.owner_pid.load(Ordering::Acquire);
            if pid != self.owner_pid {
                if !self.owner_handle.is_null() {
                    CloseHandle(self.owner_handle);
                    self.owner_handle = ptr::null_mut();
                }
                self.owner_pid = pid;
                if pid > 4 {
                    self.owner_handle =
                        OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                }
            }

            let raw_lease = shm.lease_ms.load(Ordering::Acquire);
            let lease = if raw_lease == 0 {
                DEFAULT_LEASE_MS
            } else {
                raw_lease.clamp(MIN_LEASE_MS, MAX_LEASE_MS)
            };
            let now = GetTickCount64();
            self.hard_deadline = now.saturating_add(HARD_SESSION_CAP_MS);
            self.lease_deadline = now.saturating_add(lease as u64).min(self.hard_deadline);

            SWALLOWED_DOWN_MASK.store(0, Ordering::Release);
            shm.hook_error.store(0, Ordering::Release);
            HOOK_LEASE_DEADLINE_MS.store(self.lease_deadline, Ordering::Release);
            HOOK_HARD_DEADLINE_MS.store(self.hard_deadline, Ordering::Release);
            HOOK_ARMED.store(true, Ordering::Release);
            shm.state.store(STATE_ARMED, Ordering::Release);
        }

        unsafe fn renew(&mut self, shm: &SharedState) {
            if self.hook.is_null() || !HOOK_ARMED.load(Ordering::Acquire) {
                self.disarm(shm);
                return;
            }
            let now = GetTickCount64();
            if now >= self.hard_deadline || !self.owner_alive() {
                self.disarm(shm);
                return;
            }
            let raw_lease = shm.lease_ms.load(Ordering::Acquire);
            let lease = if raw_lease == 0 {
                DEFAULT_LEASE_MS
            } else {
                raw_lease.clamp(MIN_LEASE_MS, MAX_LEASE_MS)
            };
            self.lease_deadline = now.saturating_add(lease as u64).min(self.hard_deadline);
            HOOK_LEASE_DEADLINE_MS.store(self.lease_deadline, Ordering::Release);
            shm.state.store(STATE_ARMED, Ordering::Release);
        }
    }

    unsafe fn run_guard_loop(
        shm: &SharedState,
        wake_event: *mut c_void,
        ack_event: *mut c_void,
        stop_or_parent: *mut c_void,
        ready_event: *mut c_void,
    ) {
        ACTIVE_SHM_PTR.store(shm as *const SharedState as usize, Ordering::Release);

        let mut msg = Msg {
            hwnd: ptr::null_mut(),
            message: 0,
            w_param: 0,
            l_param: 0,
            time: 0,
            pt: Point { x: 0, y: 0 },
            l_private: 0,
        };
        PeekMessageW(&mut msg, ptr::null_mut(), WM_USER, WM_USER, PM_NOREMOVE);
        if !ready_event.is_null() {
            SetEvent(ready_event);
        }

        let mut guards = GuardHandles::new();
        let mut last_seq = shm.ack_seq.load(Ordering::Acquire);
        let handles: [*mut c_void; 2] = [wake_event, stop_or_parent];
        let handle_count: u32 = if stop_or_parent.is_null() { 1 } else { 2 };

        loop {
            if !stop_or_parent.is_null() && WaitForSingleObject(stop_or_parent, 0) == WAIT_OBJECT_0
            {
                guards.disarm(shm);
                break;
            }

            let mut quit_received = false;
            while PeekMessageW(&mut msg, ptr::null_mut(), 0, 0, PM_REMOVE) != 0 {
                if msg.message == WM_QUIT {
                    quit_received = true;
                    break;
                }
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            if quit_received {
                guards.disarm(shm);
                break;
            }

            let seq = shm.cmd_seq.load(Ordering::Acquire);
            if seq != last_seq {
                last_seq = seq;
                let cmd = shm.command.load(Ordering::Acquire);
                match cmd {
                    CMD_ARM => guards.arm(shm),
                    CMD_RENEW => guards.renew(shm),
                    CMD_SHUTDOWN => {
                        guards.disarm(shm);
                        shm.ack_seq.store(seq, Ordering::Release);
                        SetEvent(ack_event);
                        break;
                    }
                    CMD_DISARM => guards.disarm(shm),
                    _ => guards.disarm(shm),
                }
                shm.ack_seq.store(seq, Ordering::Release);
                SetEvent(ack_event);
            }

            if !guards.hook.is_null() {
                let now = GetTickCount64();
                if now >= guards.lease_deadline
                    || now >= guards.hard_deadline
                    || !HOOK_ARMED.load(Ordering::Acquire)
                    || !guards.owner_alive()
                {
                    guards.disarm(shm);
                }
            }

            let wait_ms = if !guards.hook.is_null() {
                let now = GetTickCount64();
                let next_deadline = guards.lease_deadline.min(guards.hard_deadline);
                let rem = next_deadline.saturating_sub(now);
                (rem as u32).clamp(1, 50)
            } else {
                INFINITE
            };

            let res = MsgWaitForMultipleObjectsEx(
                handle_count,
                handles.as_ptr(),
                wait_ms,
                QS_ALLINPUT,
                MWMO_INPUTAVAILABLE,
            );
            if handle_count == 2 && res == WAIT_OBJECT_0 + 1 {
                guards.disarm(shm);
                break;
            }
        }

        guards.disarm(shm);
        ACTIVE_SHM_PTR.store(0, Ordering::Release);
    }

    struct ServerRuntime {
        channel_prefix: String,
        stop_event_raw: usize,
        join_handle: Option<std::thread::JoinHandle<()>>,
    }

    static SERVER_RUNTIME: OnceLock<Mutex<Option<ServerRuntime>>> = OnceLock::new();
    static CHANNEL_SEQ: AtomicU32 = AtomicU32::new(1);

    pub fn ensure_server_channel() -> Option<String> {
        let lock = SERVER_RUNTIME.get_or_init(|| Mutex::new(None));
        let mut guard = lock.lock().ok()?;
        if let Some(ref existing) = *guard {
            return Some(existing.channel_prefix.clone());
        }

        let seq = CHANNEL_SEQ.fetch_add(1, Ordering::Relaxed);
        let tick = unsafe { GetTickCount64() };
        let prefix = format!(
            "Local\\MaaNikkiMG_{}_{}_{}",
            std::process::id(),
            seq,
            tick & 0xFFFF_FFFF
        );

        let shm_w = to_wide(&format!("{prefix}_shm"));
        let wake_w = to_wide(&format!("{prefix}_wake"));
        let ack_w = to_wide(&format!("{prefix}_ack"));

        unsafe {
            let h_map = CreateFileMappingW(
                (-1isize) as *mut c_void,
                ptr::null_mut(),
                PAGE_READWRITE,
                0,
                std::mem::size_of::<SharedState>() as u32,
                shm_w.as_ptr(),
            );
            if h_map.is_null() {
                return None;
            }
            let view = MapViewOfFile(
                h_map,
                FILE_MAP_ALL_ACCESS,
                0,
                0,
                std::mem::size_of::<SharedState>(),
            );
            if view.is_null() {
                CloseHandle(h_map);
                return None;
            }
            ptr::write_bytes(view, 0, std::mem::size_of::<SharedState>());
            let shm_ref = &*(view as *const SharedState);
            ptr::write(view as *mut u32, SHM_MAGIC);
            ptr::write((view as *mut u32).add(1), SHM_VERSION);
            let _ = shm_ref;

            let wake_ev = CreateEventW(ptr::null_mut(), 0, 0, wake_w.as_ptr());
            let ack_ev = CreateEventW(ptr::null_mut(), 0, 0, ack_w.as_ptr());
            let stop_ev = CreateEventW(ptr::null_mut(), 1, 0, ptr::null());
            let ready_ev = CreateEventW(ptr::null_mut(), 1, 0, ptr::null());
            if wake_ev.is_null() || ack_ev.is_null() || stop_ev.is_null() || ready_ev.is_null() {
                if !wake_ev.is_null() {
                    CloseHandle(wake_ev);
                }
                if !ack_ev.is_null() {
                    CloseHandle(ack_ev);
                }
                if !stop_ev.is_null() {
                    CloseHandle(stop_ev);
                }
                if !ready_ev.is_null() {
                    CloseHandle(ready_ev);
                }
                UnmapViewOfFile(view);
                CloseHandle(h_map);
                return None;
            }

            let h_map_raw = h_map as usize;
            let view_raw = view as usize;
            let wake_raw = wake_ev as usize;
            let ack_raw = ack_ev as usize;
            let stop_raw = stop_ev as usize;
            let ready_raw = ready_ev as usize;

            let join = std::thread::Builder::new()
                .name("maanikki-mouse-guard".to_string())
                .spawn(move || {
                    let shm = &*(view_raw as *const SharedState);
                    run_guard_loop(
                        shm,
                        wake_raw as *mut c_void,
                        ack_raw as *mut c_void,
                        stop_raw as *mut c_void,
                        ready_raw as *mut c_void,
                    );
                    CloseHandle(wake_raw as *mut c_void);
                    CloseHandle(ack_raw as *mut c_void);
                    UnmapViewOfFile(view_raw as *const c_void);
                    CloseHandle(h_map_raw as *mut c_void);
                });

            match join {
                Ok(handle) => {
                    WaitForSingleObject(ready_ev, 1000);
                    CloseHandle(ready_ev);
                    *guard = Some(ServerRuntime {
                        channel_prefix: prefix.clone(),
                        stop_event_raw: stop_raw,
                        join_handle: Some(handle),
                    });
                    Some(prefix)
                }
                Err(_) => {
                    CloseHandle(ready_ev);
                    CloseHandle(stop_ev);
                    CloseHandle(wake_ev);
                    CloseHandle(ack_ev);
                    UnmapViewOfFile(view);
                    CloseHandle(h_map);
                    None
                }
            }
        }
    }

    pub fn shutdown_server() {
        let Some(lock) = SERVER_RUNTIME.get() else {
            return;
        };
        let Ok(mut guard) = lock.lock() else {
            return;
        };
        let Some(mut srv) = guard.take() else {
            return;
        };
        unsafe {
            let stop_ev = srv.stop_event_raw as *mut c_void;
            if !stop_ev.is_null() {
                SetEvent(stop_ev);
            }
            if let Some(handle) = srv.join_handle.take() {
                let _ = handle.join();
            }
            if !stop_ev.is_null() {
                CloseHandle(stop_ev);
            }
        }
    }

    pub fn maybe_run_cli() -> Option<i32> {
        std::hint::black_box(&CAPABILITY_MARKER);
        let mut args = std::env::args_os();
        let _exe = args.next()?;
        let mode = args.next()?;
        if mode != OsString::from("--mouse-guard") {
            return None;
        }
        let prefix = args.next()?.to_string_lossy().into_owned();
        let parent_pid = args
            .next()
            .and_then(|s| s.to_string_lossy().parse::<u32>().ok())
            .unwrap_or(0);
        if prefix.is_empty() {
            return Some(1);
        }

        let shm_w = to_wide(&format!("{prefix}_shm"));
        let wake_w = to_wide(&format!("{prefix}_wake"));
        let ack_w = to_wide(&format!("{prefix}_ack"));

        unsafe {
            let h_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, 0, shm_w.as_ptr());
            if h_map.is_null() {
                return Some(2);
            }
            let view = MapViewOfFile(
                h_map,
                FILE_MAP_ALL_ACCESS,
                0,
                0,
                std::mem::size_of::<SharedState>(),
            );
            if view.is_null() {
                CloseHandle(h_map);
                return Some(3);
            }
            let shm = &*(view as *const SharedState);
            if shm.magic != SHM_MAGIC || shm.version != SHM_VERSION {
                UnmapViewOfFile(view);
                CloseHandle(h_map);
                return Some(4);
            }
            let wake_ev = OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, 0, wake_w.as_ptr());
            let ack_ev = OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, 0, ack_w.as_ptr());
            if wake_ev.is_null() || ack_ev.is_null() {
                if !wake_ev.is_null() {
                    CloseHandle(wake_ev);
                }
                if !ack_ev.is_null() {
                    CloseHandle(ack_ev);
                }
                UnmapViewOfFile(view);
                CloseHandle(h_map);
                return Some(5);
            }

            let parent_handle = if parent_pid > 4 {
                OpenProcess(
                    SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                    0,
                    parent_pid,
                )
            } else {
                ptr::null_mut()
            };

            run_guard_loop(shm, wake_ev, ack_ev, parent_handle, ptr::null_mut());

            if !parent_handle.is_null() {
                CloseHandle(parent_handle);
            }
            CloseHandle(wake_ev);
            CloseHandle(ack_ev);
            UnmapViewOfFile(view);
            CloseHandle(h_map);
        }
        Some(0)
    }
}

#[cfg(not(windows))]
mod imp {
    pub fn ensure_server_channel() -> Option<String> {
        None
    }

    pub fn shutdown_server() {}

    pub fn maybe_run_cli() -> Option<i32> {
        None
    }
}

pub use imp::{ensure_server_channel, maybe_run_cli, shutdown_server};
