//! 文件操作命令
//!
//! 提供本地文件读取和路径检查功能

use log::debug;
use std::io::{self, BufRead, BufWriter, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use super::utils::{get_app_data_dir, get_exe_directory, normalize_path};

/// 导出 zip 的大小上限（字节）。GitHub issue 附件上限为 25 MB，余量用于导出时仍在增长的日志。
const MAX_ARCHIVE_BYTES: u64 = 24_500_000;
/// 配置和 MaaFW 以外的文本日志最多占用的预算；它们体积小，却是定位问题的基本上下文。
const CONTEXT_BUDGET_BYTES: u64 = 4_500_000;
/// 优先留给失败现场图片的预算。其余预算保证最新运行的 MaaFW 日志，没用完的会互相补位。
const IMAGE_BUDGET_BYTES: u64 = 10_000_000;
/// 剩余预算低于此值时不再保留日志结尾，这么短的片段不足以覆盖一次任务。
const MIN_LOG_TAIL_BYTES: u64 = 256 * 1024;
/// 为导出清单预留的字节数，足够容纳 `MAX_MANIFEST_OMITTED_ROWS` 行未压缩文本。
const MANIFEST_RESERVE_BYTES: u64 = 128 * 1024;
/// 清单里最多列出的被舍弃文件数。
const MAX_MANIFEST_OMITTED_ROWS: usize = 500;
/// EOCD 记录（zip 末尾）固定大小。
const ZIP_EOCD_BYTES: u64 = 22;
/// 中央目录每条记录的固定字段大小（不含文件名）。
const ZIP_CENTRAL_DIR_FIXED_BYTES: u64 = 46;
/// 保留最近 N 次导出（含本次），多余的会在每次导出完成后清理。
const MAX_EXPORTS_TO_KEEP: usize = 5;
/// 日志目录名（位于应用数据目录下）。
const DEBUG_DIR: &str = "debug";
/// 日志导出产物目录名，与 `debug/` 同级，避免下次导出把上次的产物扫进去。
const DEBUG_EXPORTS_DIR: &str = "debug_exports";

#[derive(Clone)]
struct ExportEntry {
    source_path: PathBuf,
    archive_name: String,
}

/// 包装真实 writer，统计已写字节数，用于在写入过程中实时查询压缩包大小。
struct CountingWriter<W: Write + Seek> {
    inner: W,
    counter: Arc<AtomicU64>,
}

impl<W: Write + Seek> CountingWriter<W> {
    fn new(inner: W) -> (Self, Arc<AtomicU64>) {
        let counter = Arc::new(AtomicU64::new(0));
        (
            Self {
                inner,
                counter: counter.clone(),
            },
            counter,
        )
    }
}

impl<W: Write + Seek> Write for CountingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.counter.fetch_add(n as u64, Ordering::Relaxed);
        Ok(n)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

impl<W: Write + Seek> Seek for CountingWriter<W> {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        // counter 是已写字节的**上界**：zip crate 会 seek 回去重写 local header（CRC、
        // 压缩前后大小），重写的字节被 write() 重复计入；写入失败留下的占位 header
        // 也已经进了 counter。上界对预算判断是安全的（只会更早停止写入）。
        self.inner.seek(pos)
    }
}

fn add_file_to_zip<W>(
    zip: &mut zip::ZipWriter<W>,
    path: &Path,
    archive_name: &str,
    options: zip::write::SimpleFileOptions,
    prepared: Option<&[u8]>,
) -> bool
where
    W: Write + Seek,
{
    use std::fs::File;

    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(e) => {
            log::warn!("无法打开文件 {:?}: {}", path, e);
            return false;
        }
    };

    if let Err(e) = zip.start_file(archive_name, options) {
        log::warn!("创建 zip 条目失败 {}: {}", archive_name, e);
        return false;
    }

    let copied = if let Some(bytes) = prepared {
        zip.write_all(bytes)
    } else {
        io::copy(&mut file, zip).map(|_| ())
    };
    if let Err(e) = copied {
        log::warn!("写入 zip 失败 {}: {}", archive_name, e);
        return false;
    }

    true
}

/// zip 每条目的 local header 固定开销（30）+ DEFLATE 帧头 + 余量。
/// 文件名部分在 local header 和中央目录各出现一次，由 `BudgetedZip::entry_overhead` 计入。
const ZIP_LOCAL_HEADER_OVERHEAD: u64 = 64;

/// 计算 deflate 后的字节数——与 zip crate 内部压缩同算法。数据直接丢弃，不占内存。
fn deflated_size(reader: &mut impl io::Read) -> io::Result<u64> {
    use flate2::write::DeflateEncoder;
    use flate2::Compression;

    let mut encoder = DeflateEncoder::new(io::sink(), Compression::default());
    io::copy(reader, &mut encoder)?;
    encoder.try_finish()?;
    Ok(encoder.total_out())
}

/// 从 `start` 打开日志，并跳到下一行开头，避免结尾片段以半行开始。
fn open_log_tail(path: &Path, start: u64) -> io::Result<io::BufReader<std::fs::File>> {
    let mut file = std::fs::File::open(path)?;
    file.seek(SeekFrom::Start(start))?;
    let mut reader = io::BufReader::new(file);
    if start > 0 {
        reader.read_until(b'\n', &mut Vec::new())?;
    }
    Ok(reader)
}

fn sort_newest_first(entries: &mut [ExportEntry]) {
    entries.sort_by_cached_key(|entry| {
        std::cmp::Reverse(entry.source_path.metadata().and_then(|m| m.modified()).ok())
    });
}

enum AddOutcome {
    Added,
    /// 放不下；日志会带上压缩后大小，供估算可保留的结尾长度。
    TooLarge {
        compressed: u64,
    },
    Failed,
}

enum LogFit {
    Whole,
    /// 只放下了结尾，预算已用尽。
    Tail,
    /// 预算已用尽，一点也放不下。
    Omitted,
    /// 文件读不了（比如被占用），不代表预算用尽。
    Unreadable,
}

/// 写入前先算出条目的实际占用，保证压缩包不超过调用方给的累计上限。
struct BudgetedZip<W: Write + Seek> {
    zip: zip::ZipWriter<CountingWriter<W>>,
    written: Arc<AtomicU64>,
    /// finish() 时才写入的 EOCD、清单和累计的中央目录字节。
    finish_reserve: u64,
    options: zip::write::SimpleFileOptions,
    added: usize,
    truncated: Vec<String>,
}

impl<W: Write + Seek> BudgetedZip<W> {
    fn new(writer: W) -> Self {
        let (counting, written) = CountingWriter::new(writer);
        Self {
            zip: zip::ZipWriter::new(counting),
            written,
            finish_reserve: ZIP_EOCD_BYTES + MANIFEST_RESERVE_BYTES,
            options: zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated),
            added: 0,
            truncated: Vec::new(),
        }
    }

    fn used_bytes(&self) -> u64 {
        self.written
            .load(Ordering::Relaxed)
            .saturating_add(self.finish_reserve)
    }

    fn entry_overhead(archive_name: &str) -> u64 {
        ZIP_LOCAL_HEADER_OVERHEAD + ZIP_CENTRAL_DIR_FIXED_BYTES + 2 * archive_name.len() as u64
    }

    /// 上限内还能放下多少压缩后数据。
    fn available(&self, archive_name: &str, limit: u64) -> u64 {
        limit
            .saturating_sub(self.used_bytes())
            .saturating_sub(Self::entry_overhead(archive_name))
    }

    fn record_added(&mut self, archive_name: &str) {
        self.finish_reserve = self
            .finish_reserve
            .saturating_add(ZIP_CENTRAL_DIR_FIXED_BYTES + archive_name.len() as u64);
        self.added += 1;
    }

    fn try_add(&mut self, entry: &ExportEntry, limit: u64) -> AddOutcome {
        let available = self.available(&entry.archive_name, limit);
        let path = &entry.source_path;
        let mut prepared = None;
        let mut options = self.options;

        if is_image_file(path) {
            // 图片已压缩，先用原始大小排除放不下的，免得逐张解码 PNG。
            let Ok(raw_size) = path.metadata().map(|m| m.len()) else {
                return AddOutcome::Failed;
            };
            if raw_size > available {
                return AddOutcome::TooLarge {
                    compressed: raw_size,
                };
            }
            if has_extension(path, &["png"]) {
                let sanitized = std::fs::read(path)
                    .map_err(|error| error.to_string())
                    .and_then(|bytes| super::diagnostic_images::sanitize_png(&bytes));
                match sanitized {
                    Ok(bytes) if bytes.len() as u64 > available => {
                        return AddOutcome::TooLarge {
                            compressed: bytes.len() as u64,
                        };
                    }
                    Ok(bytes) => prepared = Some(bytes),
                    Err(error) => {
                        log::warn!("跳过无法处理的诊断截图 {}: {}", entry.archive_name, error);
                        return AddOutcome::Failed;
                    }
                }
            }
            options = options.compression_method(zip::CompressionMethod::Stored);
        } else {
            let measured = std::fs::File::open(path).and_then(|mut file| deflated_size(&mut file));
            match measured {
                Ok(compressed) if compressed > available => {
                    return AddOutcome::TooLarge { compressed };
                }
                Ok(_) => {}
                Err(error) => {
                    log::warn!("无法读取文件 {:?}: {}", path, error);
                    return AddOutcome::Failed;
                }
            }
        }

        if add_file_to_zip(
            &mut self.zip,
            path,
            &entry.archive_name,
            options,
            prepared.as_deref(),
        ) {
            self.record_added(&entry.archive_name);
            AddOutcome::Added
        } else {
            AddOutcome::Failed
        }
    }

    /// 依次尝试写入，返回没有写入的条目（保持原顺序）。
    fn add_entries(&mut self, entries: Vec<ExportEntry>, limit: u64) -> Vec<ExportEntry> {
        entries
            .into_iter()
            .filter(|entry| !matches!(self.try_add(entry, limit), AddOutcome::Added))
            .collect()
    }

    /// 写入整份日志；放不下时尽量只留结尾（最新内容）。
    fn add_log_or_tail(&mut self, entry: &ExportEntry, limit: u64) -> LogFit {
        match self.try_add(entry, limit) {
            AddOutcome::Added => LogFit::Whole,
            AddOutcome::Failed => LogFit::Unreadable,
            AddOutcome::TooLarge { compressed } => {
                match self.add_log_tail(entry, compressed, limit) {
                    Ok(true) => LogFit::Tail,
                    Ok(false) => LogFit::Omitted,
                    Err(error) => {
                        log::warn!("写入日志结尾失败 {}: {}", entry.archive_name, error);
                        LogFit::Omitted
                    }
                }
            }
        }
    }

    fn add_log_tail(
        &mut self,
        entry: &ExportEntry,
        compressed: u64,
        limit: u64,
    ) -> io::Result<bool> {
        let tail_name = tail_archive_name(&entry.archive_name);
        let available = self.available(&tail_name, limit);
        if available < MIN_LOG_TAIL_BYTES {
            return Ok(false);
        }
        let raw_size = entry.source_path.metadata()?.len();
        // 留 10% 余量，让压缩率随内容变化时通常一次命中；未命中就按实测比例再缩。
        let target = available / 10 * 9;
        let mut keep = scale_bytes(raw_size, target, compressed);
        for _ in 0..3 {
            let start = raw_size.saturating_sub(keep);
            let measured = deflated_size(&mut open_log_tail(&entry.source_path, start)?)?;
            if measured > available {
                keep = scale_bytes(keep, target, measured);
                continue;
            }
            let mut reader = open_log_tail(&entry.source_path, start)?;
            self.zip
                .start_file(tail_name.as_str(), self.options)
                .map_err(io::Error::other)?;
            io::copy(&mut reader, &mut self.zip)?;
            self.record_added(&tail_name);
            self.truncated.push(format!(
                "{} keeps the last {} of {} bytes",
                tail_name, keep, raw_size
            ));
            return Ok(true);
        }
        Ok(false)
    }

    fn finish(mut self, manifest: &str) -> Result<(), String> {
        self.zip
            .start_file("export-manifest.txt", self.options)
            .map_err(|error| error.to_string())?;
        self.zip
            .write_all(manifest.as_bytes())
            .map_err(|error| error.to_string())?;
        self.zip.finish().map_err(|error| error.to_string())?;
        Ok(())
    }
}

/// 结尾片段换名，打开压缩包就能看出它不是完整文件。
fn tail_archive_name(archive_name: &str) -> String {
    match archive_name.strip_suffix(".log") {
        Some(stem) => format!("{}.tail.log", stem),
        None => format!("{}.tail", archive_name),
    }
}

/// `value * numerator / denominator`，中间结果放大到 u128 防溢出。
fn scale_bytes(value: u64, numerator: u64, denominator: u64) -> u64 {
    (u128::from(value) * u128::from(numerator) / u128::from(denominator.max(1))) as u64
}

fn normalize_archive_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn collect_files_recursively(dir: &Path, archive_prefix: &str) -> Result<Vec<ExportEntry>, String> {
    if !dir.exists() || !dir.is_dir() {
        return Ok(Vec::new());
    }

    let mut files = Vec::new();
    let mut stack = vec![dir.to_path_buf()];

    while let Some(current_dir) = stack.pop() {
        let entries = std::fs::read_dir(&current_dir)
            .map_err(|e| format!("读取目录失败 [{}]: {}", current_dir.display(), e))?;

        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            if !path.is_file() {
                continue;
            }

            let relative_path = match path.strip_prefix(dir) {
                Ok(rel) => rel,
                Err(_) => continue,
            };
            let archive_name = if archive_prefix.is_empty() {
                normalize_archive_path(relative_path)
            } else {
                format!(
                    "{}/{}",
                    archive_prefix.trim_end_matches('/'),
                    normalize_archive_path(relative_path)
                )
            };

            files.push(ExportEntry {
                source_path: path,
                archive_name,
            });
        }
    }

    files.sort_by(|a, b| a.archive_name.cmp(&b.archive_name));
    Ok(files)
}

fn is_image_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    path.extension()
        .map(|ext| {
            let ext_lower = ext.to_string_lossy().to_lowercase();
            ext_lower == "png" || ext_lower == "jpg" || ext_lower == "jpeg"
        })
        .unwrap_or(false)
}

fn has_extension(path: &Path, extensions: &[&str]) -> bool {
    path.extension()
        .map(|ext| {
            let ext_lower = ext.to_string_lossy().to_lowercase();
            extensions.iter().any(|e| ext_lower == *e)
        })
        .unwrap_or(false)
}

/// 收集 debug 目录下各子文件夹内以指定后缀结尾的文件（递归）
/// 压缩包内路径保留子文件夹层级，例如 on_error/xxx.json
fn collect_debug_subdir_files(
    debug_dir: &Path,
    extensions: &[&str],
) -> Result<Vec<ExportEntry>, String> {
    let entries = std::fs::read_dir(debug_dir)
        .map_err(|e| format!("读取日志目录失败 [{}]: {}", debug_dir.display(), e))?;

    let mut files = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Some(dir_name) = path.file_name().map(|n| n.to_string_lossy().to_string()) else {
            continue;
        };

        for export in collect_files_recursively(&path, &dir_name)? {
            if has_extension(&export.source_path, extensions) {
                files.push(export);
            }
        }
    }

    files.sort_by(|a, b| a.archive_name.cmp(&b.archive_name));
    Ok(files)
}

/// Include recent task and collection evidence stored under `logs/` outside `debug/`.
fn collect_recent_collection_files(data_dir: &Path) -> Result<Vec<ExportEntry>, String> {
    const ALLOWED_EXTS: &[&str] = &["log", "json", "png", "jpg", "jpeg"];
    const MAX_FLAT_ENTRIES_PER_KIND: usize = 60;

    let mut files = Vec::new();
    for kind in [
        "blessing-lock",
        "blessing-pursuit",
        "wardrobe",
        "daily-upgrade",
    ] {
        let directory = data_dir.join("logs").join(kind);
        if !directory.is_dir() || directory.is_symlink() {
            continue;
        }
        let mut runs: Vec<_> = std::fs::read_dir(&directory)
            .map_err(|e| format!("读取收集日志目录失败 [{}]: {}", directory.display(), e))?
            .flatten()
            .filter(|entry| entry.path().is_dir())
            .collect();
        // Run names start with YYYYMMDD_HHMMSS; retain the latest two attempts.
        runs.sort_by_key(|entry| std::cmp::Reverse(entry.file_name()));
        for run in runs.into_iter().take(2) {
            let prefix = format!("logs/{}/{}", kind, run.file_name().to_string_lossy());
            files.extend(
                collect_files_recursively(&run.path(), &prefix)?
                    .into_iter()
                    .filter(|entry| has_extension(&entry.source_path, ALLOWED_EXTS)),
            );
        }
    }

    for kind in [
        "daily",
        "navigation",
        "startup",
        "fishing",
        "minigame_bling",
        "performance",
    ] {
        let directory = data_dir.join("logs").join(kind);
        if !directory.is_dir() || directory.is_symlink() {
            continue;
        }
        let prefix = format!("logs/{}", kind);
        let mut kind_files: Vec<_> = collect_files_recursively(&directory, &prefix)?
            .into_iter()
            .filter(|entry| has_extension(&entry.source_path, ALLOWED_EXTS))
            .collect();
        sort_newest_first(&mut kind_files);
        kind_files.truncate(MAX_FLAT_ENTRIES_PER_KIND);
        files.extend(kind_files);
    }

    Ok(files)
}

pub fn resolve_local_file_path(filename: &str) -> Result<PathBuf, String> {
    let exe_dir = get_exe_directory()?;
    let file_path = normalize_path(&exe_dir.join(filename).to_string_lossy());
    // 防止路径穿越，确保仍在 exe 目录下
    if !file_path.starts_with(&exe_dir) {
        return Err(format!("非法文件路径: {}", filename));
    }
    Ok(file_path)
}

/// 读取 exe 同目录下的文本文件
#[tauri::command]
pub fn read_local_file(filename: String) -> Result<String, String> {
    let file_path = resolve_local_file_path(&filename)?;
    debug!("Reading local file: {:?}", file_path);

    std::fs::read_to_string(&file_path)
        .map_err(|e| format!("读取文件失败 [{}]: {}", file_path.display(), e))
}

/// 读取 exe 同目录下的二进制文件，返回 base64 编码
#[tauri::command]
pub fn read_local_file_base64(filename: String) -> Result<String, String> {
    use base64::{engine::general_purpose::STANDARD, Engine as _};

    let file_path = resolve_local_file_path(&filename)?;
    debug!("Reading local file (base64): {:?}", file_path);

    let data = std::fs::read(&file_path)
        .map_err(|e| format!("读取文件失败 [{}]: {}", file_path.display(), e))?;

    Ok(STANDARD.encode(&data))
}

/// 检查 exe 同目录下的文件是否存在
#[tauri::command]
pub fn local_file_exists(filename: String) -> Result<bool, String> {
    let file_path = resolve_local_file_path(&filename)?;
    Ok(file_path.exists())
}

/// 获取 exe 所在目录路径
#[tauri::command]
pub fn get_exe_dir() -> Result<String, String> {
    let exe_dir = get_exe_directory()?;
    Ok(exe_dir.to_string_lossy().to_string())
}

/// 获取应用数据目录路径
/// - macOS: ~/Library/Application Support/MXU/
/// - Windows/Linux: exe 所在目录
#[tauri::command]
pub fn get_data_dir() -> Result<String, String> {
    let data_dir = get_app_data_dir()?;
    Ok(data_dir.to_string_lossy().to_string())
}

/// 删除历史日志、错误截图、日志导出产物与任务记录（保留当前会话活跃日志、16:9 窗口还原记录与未完成任务保护目录）。
#[tauri::command]
pub async fn clear_log_files(
    exclude_file_name: Option<String>,
    confirmed: bool,
) -> Result<super::storage_budget::StorageCleanResult, String> {
    if !confirmed {
        return Err("请在设置中确认清理日志文件".to_string());
    }
    let data_dir = get_app_data_dir()?;
    tauri::async_runtime::spawn_blocking(move || {
        super::storage_budget::clear_logs_protected(&data_dir, exclude_file_name.as_deref())
    })
    .await
    .map_err(|error| error.to_string())
}

/// 清理超过保留期（7 / 30 / 90 天）的文件，并始终执行各目录安全滚动配额控制。
#[tauri::command]
pub async fn cleanup_expired_logs(
    retention_days: u32,
    exclude_file_name: Option<String>,
) -> Result<super::storage_budget::StorageCleanResult, String> {
    if ![0, 7, 30, 90].contains(&retention_days) {
        return Err("日志保留时间应为 0、7、30 或 90 天".to_string());
    }
    let data_dir = get_app_data_dir()?;
    tauri::async_runtime::spawn_blocking(move || {
        let scope =
            super::storage_budget::ProtectedScope::collect(&data_dir, exclude_file_name.as_deref());
        let mut total = super::storage_budget::StorageCleanResult::default();
        if retention_days > 0 {
            let cutoff = std::time::SystemTime::now()
                - std::time::Duration::from_secs(u64::from(retention_days) * 86400);
            for name in [DEBUG_DIR, "logs", DEBUG_EXPORTS_DIR] {
                let dir = data_dir.join(name);
                let removed =
                    super::storage_budget::remove_expired_protected(&dir, cutoff, &scope, false);
                total.deleted_files = total.deleted_files.saturating_add(removed.deleted_files);
                total.freed_bytes = total.freed_bytes.saturating_add(removed.freed_bytes);
            }
        }
        let budgeted = super::storage_budget::enforce_storage_budgets_with_scope(&data_dir, &scope);
        total.deleted_files = total.deleted_files.saturating_add(budgeted.deleted_files);
        total.freed_bytes = total.freed_bytes.saturating_add(budgeted.freed_bytes);
        total
    })
    .await
    .map_err(|error| error.to_string())
}

/// 统计数据目录各分类存储占用。
#[tauri::command]
pub async fn get_storage_usage() -> Result<super::storage_budget::StorageUsage, String> {
    let data_dir = get_app_data_dir()?;
    tauri::async_runtime::spawn_blocking(move || {
        super::storage_budget::compute_storage_usage(&data_dir)
    })
    .await
    .map_err(|error| error.to_string())
}

/// 清理可再生缓存（图文攻略缓存、过期下载临时文件及遗留安装包），保留内置运行库与账号配置。
#[tauri::command]
pub async fn clear_storage_cache(
    confirmed: bool,
) -> Result<super::storage_budget::StorageCleanResult, String> {
    if !confirmed {
        return Err("请在设置中确认清理临时缓存".to_string());
    }
    let data_dir = get_app_data_dir()?;
    tauri::async_runtime::spawn_blocking(move || super::storage_budget::clear_cache_dir(&data_dir))
        .await
        .map_err(|error| error.to_string())
}

/// 获取当前工作目录
#[tauri::command]
pub fn get_cwd() -> Result<String, String> {
    std::env::current_dir()
        .map(|p| p.to_string_lossy().to_string())
        .map_err(|e| format!("Failed to get current directory: {}", e))
}

/// 检查 exe 路径是否存在问题
/// 返回: None 表示正常, Some("root") 表示在磁盘根目录, Some("temp") 表示在临时目录
#[tauri::command]
pub fn check_exe_path() -> Option<String> {
    let exe_dir = match get_exe_directory() {
        Ok(dir) => dir,
        Err(_) => return None,
    };

    let path_str = exe_dir.to_string_lossy().to_lowercase();

    // 检查是否在磁盘根目录（如 C:\, D:\ 等）
    // Windows 根目录特征：路径只有盘符和反斜杠，如 "c:\" 或 "d:\"
    if exe_dir.parent().is_none() || exe_dir.parent() == Some(std::path::Path::new("")) {
        return Some("root".to_string());
    }

    // Windows 下额外检查：盘符根目录（如 C:\）
    #[cfg(target_os = "windows")]
    {
        let components: Vec<_> = exe_dir.components().collect();
        // 根目录只有一个组件（盘符前缀）
        if components.len() == 1 {
            return Some("root".to_string());
        }
    }

    // 检查是否在临时目录
    // 常见的临时目录特征
    let temp_indicators = [
        "\\temp\\",
        "/temp/",
        "\\tmp\\",
        "/tmp/",
        "\\appdata\\local\\temp",
        "/appdata/local/temp",
        // Windows 压缩包临时解压目录
        "\\temporary internet files\\",
        // 7-Zip 临时目录
        "\\7zocab",
        "\\7zo",
        // WinRAR 临时目录
        "\\rar$",
        // WinZip 临时目录
        "\\wz",
        // 360压缩临时目录
        "\\360zip$",
        "\\360zip_tmp",
        "\\360xtract",
        // 2345好压临时目录
        "\\2345zip",
        "\\haozip$",
        // 快压临时目录
        "\\kuaizip$",
        // Bandizip 临时目录
        "\\bztmp",
        "\\bandizip$",
        // 百度网盘下载临时目录
        "\\baiduyundownload",
        "\\baidupcs",
        // 迅雷下载临时目录
        "\\thundernetwork",
        "\\xunlei\\downloads\\.tmp",
        // QQ/微信 文件临时目录
        "\\tencent\\qq\\temp",
        "\\tencent files\\",
        "\\weixin files\\",
        // 通用临时目录特征
        "\\temp_",
        "\\.tmp\\",
    ];

    for indicator in &temp_indicators {
        if path_str.contains(indicator) {
            return Some("temp".to_string());
        }
    }

    // 检查系统临时目录
    if let Ok(temp_dir) = std::env::var("TEMP") {
        let temp_lower = temp_dir.to_lowercase();
        if path_str.starts_with(&temp_lower) {
            return Some("temp".to_string());
        }
    }
    if let Ok(tmp_dir) = std::env::var("TMP") {
        let tmp_lower = tmp_dir.to_lowercase();
        if path_str.starts_with(&tmp_lower) {
            return Some("temp".to_string());
        }
    }

    None
}

/// 为文件设置可执行权限（仅 Unix 系统）
/// Windows 上此命令不做任何操作
#[tauri::command]
pub fn set_executable(file_path: String) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let metadata = std::fs::metadata(&file_path)
            .map_err(|e| format!("无法获取文件元数据 [{}]: {}", file_path, e))?;
        let mut permissions = metadata.permissions();
        // 添加可执行权限 (owner, group, others)
        let mode = permissions.mode() | 0o111;
        permissions.set_mode(mode);
        std::fs::set_permissions(&file_path, permissions)
            .map_err(|e| format!("无法设置执行权限 [{}]: {}", file_path, e))?;
        log::info!("Set executable permission: {}", file_path);
    }
    #[cfg(not(unix))]
    {
        let _ = file_path; // 避免未使用警告
    }
    Ok(())
}

/// 从指定目录收集图片，按 mtime 从新到旧排序。不做任何大小截断。
fn collect_debug_images(dir: &Path, archive_prefix: &str) -> Vec<ExportEntry> {
    if !dir.exists() || !dir.is_dir() {
        return Vec::new();
    }
    let rd = match std::fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(e) => {
            // 目录不存在的情况上面已经 return 了，所以走到这里通常是权限或 IO 问题。
            log::warn!(
                "读取 {} 目录失败 [{}]: {}",
                archive_prefix,
                dir.display(),
                e
            );
            return Vec::new();
        }
    };

    let mut images: Vec<_> = rd.flatten().filter(|e| is_image_file(&e.path())).collect();

    images.sort_by(|a, b| {
        let time_a = a.metadata().and_then(|m| m.modified()).ok();
        let time_b = b.metadata().and_then(|m| m.modified()).ok();
        time_b.cmp(&time_a)
    });

    let mut entries = Vec::with_capacity(images.len());
    for entry in images {
        let path = entry.path();
        let Some(name) = path.file_name() else {
            continue;
        };
        let archive_name = format!("{}/{}", archive_prefix, name.to_string_lossy());
        entries.push(ExportEntry {
            source_path: path,
            archive_name,
        });
    }
    entries
}

/// 导出日志文件为单个 zip，体积不超过 `MAX_ARCHIVE_BYTES`，返回 zip 路径。
///
/// 超出部分按时间从旧到新舍弃，被舍弃的文件列在包内的 `export-manifest.txt`。
///
/// 注：`vision/` 是否有内容由 `maa_set_save_draw` 控制；导出时只要 `vision/`
/// 下有文件就参与打包，因此本命令不接收 save_draw 参数。
#[tauri::command]
pub async fn export_logs(
    project_name: Option<String>,
    project_version: Option<String>,
) -> Result<String, String> {
    tokio::task::spawn_blocking(move || export_logs_blocking(project_name, project_version))
        .await
        .map_err(|e| format!("导出任务执行失败: {}", e))?
}

fn export_logs_blocking(
    project_name: Option<String>,
    project_version: Option<String>,
) -> Result<String, String> {
    use std::fs::File;

    // 日志在数据目录下（macOS: ~/Library/Application Support/MXU/debug）
    let data_dir = get_app_data_dir()?;
    let debug_dir = data_dir.join(DEBUG_DIR);

    if !debug_dir.exists() {
        return Err("日志目录不存在".to_string());
    }

    let now = chrono::Local::now();
    let date_str = now.format("%Y%m%d-%H%M%S");
    let name = project_name.unwrap_or_else(|| "mxu".to_string());
    let version = project_version.unwrap_or_default();
    let dir_name = if version.is_empty() {
        format!("{}-logs-{}", name, date_str)
    } else {
        format!("{}-logs-{}-{}", name, version, date_str)
    };
    // 产物放在 debug_exports/ 下而不是 debug/，避免下次导出把上次的产物扫进去。
    let exports_root = data_dir.join(DEBUG_EXPORTS_DIR);
    let out_dir = exports_root.join(&dir_name);
    std::fs::create_dir_all(&out_dir)
        .map_err(|e| format!("创建导出目录失败 [{}]: {}", out_dir.display(), e))?;

    // ─── 1. 收集：MaaFW 日志占体积绝大部分，与其余文本日志分开排优先级 ───
    let mut maafw_logs: Vec<ExportEntry> = Vec::new();
    let mut other_logs: Vec<ExportEntry> = Vec::new();

    let entries = std::fs::read_dir(&debug_dir).map_err(|e| format!("读取日志目录失败: {}", e))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        if !has_extension(&path, &["log", "dmp"]) {
            continue;
        }
        let Some(archive_name) = path.file_name().map(|n| n.to_string_lossy().to_string()) else {
            continue;
        };
        let target = if archive_name.starts_with("maafw") && has_extension(&path, &["log"]) {
            &mut maafw_logs
        } else {
            &mut other_logs
        };
        target.push(ExportEntry {
            source_path: path,
            archive_name,
        });
    }

    other_logs.extend(collect_debug_subdir_files(
        &debug_dir,
        &["log", "json", "dmp"],
    )?);
    let collection_entries = collect_recent_collection_files(&data_dir)?;
    other_logs.extend(
        collection_entries
            .iter()
            .filter(|entry| has_extension(&entry.source_path, &["log", "json"]))
            .cloned(),
    );
    let mut images: Vec<_> = collection_entries
        .into_iter()
        .filter(|entry| is_image_file(&entry.source_path))
        .collect();
    images.extend(collect_debug_images(
        &debug_dir.join("on_error"),
        "on_error",
    ));
    images.extend(collect_debug_images(&debug_dir.join("vision"), "vision"));

    sort_newest_first(&mut maafw_logs);
    sort_newest_first(&mut other_logs);
    sort_newest_first(&mut images);

    // 配置从小到大：小文件先进，账号存档这类大文件放不下时不挤占其它配置。
    let mut context = collect_files_recursively(&data_dir.join("config"), "config")?;
    context.sort_by_key(|entry| entry.source_path.metadata().map_or(u64::MAX, |m| m.len()));
    context.extend(other_logs);

    let total_entries = context.len() + maafw_logs.len() + images.len();
    if total_entries == 0 {
        return Err("没有可导出的日志文件".to_string());
    }

    // ─── 2. 按优先级写入：放不下的留到最后一轮，用其它类别没用完的预算再试一次 ───
    let archive_path = out_dir.join(format!("{}.zip", dir_name));
    let file = File::create(&archive_path)
        .map_err(|e| format!("创建压缩包失败 [{}]: {}", archive_path.display(), e))?;
    // 64 KB 缓冲：deflate 输出的 chunk 经常几十到几百 KB，默认 8 KB 太小。
    let mut archive = BudgetedZip::new(BufWriter::with_capacity(64 * 1024, file));

    let mut omitted_context = archive.add_entries(context, CONTEXT_BUDGET_BYTES);

    // 最新一份 MaaFW 日志必须留下，但要为失败现场图片让出预算。
    let mut maafw_logs = maafw_logs.into_iter();
    let mut omitted_maafw = Vec::new();
    let mut maafw_full = false;
    if let Some(newest) = maafw_logs.next() {
        let fit = archive.add_log_or_tail(&newest, MAX_ARCHIVE_BYTES - IMAGE_BUDGET_BYTES);
        maafw_full = !matches!(fit, LogFit::Whole | LogFit::Unreadable);
        if matches!(fit, LogFit::Unreadable | LogFit::Omitted) {
            omitted_maafw.push(newest);
        }
    }

    let image_limit = archive.used_bytes().saturating_add(IMAGE_BUDGET_BYTES);
    let mut omitted_images = archive.add_entries(images, image_limit.min(MAX_ARCHIVE_BYTES));

    // 更早的 MaaFW 日志按时间连续保留：第一份放不下的只留结尾，再往前的全部舍弃，
    // 时间线上不会出现跳过大文件造成的空洞。
    for log in maafw_logs {
        if maafw_full {
            omitted_maafw.push(log);
            continue;
        }
        let fit = archive.add_log_or_tail(&log, MAX_ARCHIVE_BYTES);
        maafw_full = !matches!(fit, LogFit::Whole | LogFit::Unreadable);
        if matches!(fit, LogFit::Unreadable | LogFit::Omitted) {
            omitted_maafw.push(log);
        }
    }

    omitted_images = archive.add_entries(omitted_images, MAX_ARCHIVE_BYTES);
    omitted_context = archive.add_entries(omitted_context, MAX_ARCHIVE_BYTES);

    // ─── 3. 写入清单并收尾 ───
    let mut omitted = omitted_context;
    omitted.extend(omitted_maafw);
    omitted.extend(omitted_images);
    sort_newest_first(&mut omitted);

    let added = archive.added;
    let manifest = build_export_manifest(&now, added, &archive.truncated, &omitted);
    archive
        .finish(&manifest)
        .map_err(|e| format!("完成压缩失败 [{}]: {}", archive_path.display(), e))?;

    log::info!(
        "日志导出完成：写入 {} 个文件，舍弃 {} 个较早文件，{} bytes，输出 {}",
        added,
        omitted.len(),
        std::fs::metadata(&archive_path).map_or(0, |m| m.len()),
        archive_path.display()
    );

    // 只保留最近 MAX_EXPORTS_TO_KEEP 次导出。清理失败仅 warn，不影响本次结果。
    prune_old_exports(&exports_root, &out_dir);

    Ok(archive_path.to_string_lossy().to_string())
}

fn build_export_manifest(
    now: &chrono::DateTime<chrono::Local>,
    added: usize,
    truncated: &[String],
    omitted: &[ExportEntry],
) -> String {
    use std::fmt::Write as _;

    let omitted_bytes: u64 = omitted
        .iter()
        .filter_map(|entry| entry.source_path.metadata().ok())
        .map(|metadata| metadata.len())
        .sum();
    let mut text = format!(
        "Exported: {}\nIncluded files: {}\nOmitted files: {} ({:.1} MB before compression)\n\
         Archive limit: {:.1} MB; newest logs and failure evidence are kept first.\n",
        now.format("%Y-%m-%d %H:%M:%S %z"),
        added,
        omitted.len(),
        omitted_bytes as f64 / 1e6,
        MAX_ARCHIVE_BYTES as f64 / 1e6,
    );
    for note in truncated {
        let _ = writeln!(text, "Truncated: {}", note);
    }
    if !omitted.is_empty() {
        let _ = writeln!(
            text,
            "\nOmitted, newest first (modified, bytes, path; at most {} rows):",
            MAX_MANIFEST_OMITTED_ROWS
        );
    }
    for entry in omitted.iter().take(MAX_MANIFEST_OMITTED_ROWS) {
        let metadata = entry.source_path.metadata().ok();
        let modified = metadata
            .as_ref()
            .and_then(|m| m.modified().ok())
            .map(chrono::DateTime::<chrono::Local>::from)
            .map_or_else(
                || "unknown".to_string(),
                |time| time.format("%Y-%m-%d %H:%M:%S").to_string(),
            );
        let _ = writeln!(
            text,
            "{}\t{}\t{}",
            modified,
            metadata.map_or(0, |m| m.len()),
            entry.archive_name
        );
    }
    text
}

/// 扫描 `exports_root` 下子目录，按目录名里的时间戳新→旧排序，
/// 删除超出 `MAX_EXPORTS_TO_KEEP` 的旧目录。`current_export` 始终保留。
///
/// 用目录名解时间戳而不是 mtime：Windows 上目录 mtime 不稳定，
/// 且备份/同步工具可能改写。
fn prune_old_exports(exports_root: &Path, current_export: &Path) {
    let rd = match std::fs::read_dir(exports_root) {
        Ok(rd) => rd,
        Err(e) => {
            log::warn!(
                "枚举导出目录失败 [{}]: {}，跳过旧导出清理",
                exports_root.display(),
                e
            );
            return;
        }
    };

    // 解不出时间戳的子目录直接跳过（既不删也不算名额）。
    let mut dirs: Vec<(PathBuf, String)> = rd
        .flatten()
        .filter(|e| e.path().is_dir())
        .filter(|e| e.path() != current_export)
        .filter_map(|e| {
            let path = e.path();
            let name = path.file_name()?.to_string_lossy().into_owned();
            let ts = parse_export_timestamp(&name)?;
            Some((path, ts))
        })
        .collect();

    // 本次产物始终保留，所以历史最多留 MAX_EXPORTS_TO_KEEP - 1 个
    let keep_others = MAX_EXPORTS_TO_KEEP - 1;
    if dirs.len() <= keep_others {
        return;
    }

    // 时间戳字符串的字典序就是时间序
    dirs.sort_by(|a, b| b.1.cmp(&a.1));
    for (path, _) in dirs.into_iter().skip(keep_others) {
        match std::fs::remove_dir_all(&path) {
            Ok(()) => log::info!("已清理旧导出: {}", path.display()),
            Err(e) => log::warn!("清理旧导出失败 [{}]: {}", path.display(), e),
        }
    }
}

/// 从导出目录名末尾解出 `YYYYMMDD-HHMMSS` 时间戳作为排序键。
fn parse_export_timestamp(dir_name: &str) -> Option<String> {
    const TS_LEN: usize = 15;
    // 在字节层面取末尾，避免 dir_name 含非 ASCII 时被切到 UTF-8 码点中间 panic
    let bytes = dir_name.as_bytes();
    if bytes.len() < TS_LEN {
        return None;
    }
    let tail = &bytes[bytes.len() - TS_LEN..];
    let shape_ok = tail[..8].iter().all(|b| b.is_ascii_digit())
        && tail[8] == b'-'
        && tail[9..].iter().all(|b| b.is_ascii_digit());
    if !shape_ok {
        return None;
    }
    // 形状校验通过 ⇒ 全是 ASCII，from_utf8 必然成功
    Some(std::str::from_utf8(tail).ok()?.to_string())
}
