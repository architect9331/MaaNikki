//! Verified file-level MaaNikki patches with mirrored backups and rollback.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};

#[derive(Deserialize)]
struct Patch {
    maanikki_patch: u32,
    from_version: String,
    to_version: String,
    added: Vec<String>,
    modified: Vec<String>,
    deleted: Vec<String>,
    base_files: BTreeMap<String, String>,
    target_files: BTreeMap<String, String>,
}

fn relative_path(name: &str) -> Result<PathBuf, String> {
    let parts: Vec<_> = name.split('/').collect();
    if parts.iter().any(|part| {
        part.is_empty()
            || *part == "."
            || *part == ".."
            || part.ends_with(['.', ' '])
            || part
                .chars()
                .any(|c| c.is_control() || "\\:<>\"|?*".contains(c))
    }) || [
        "config",
        "cache",
        "logs",
        "debug",
        ".git",
        ".build",
        "artifacts",
    ]
    .contains(&parts[0].to_lowercase().as_str())
    {
        return Err(format!("更新包文件路径无效：{name}"));
    }
    Ok(parts.iter().collect())
}

fn checked_join(root: &Path, name: &str) -> Result<PathBuf, String> {
    let relative = relative_path(name)?;
    let mut path = root.to_path_buf();
    for part in relative.components() {
        path.push(part);
        if let Ok(metadata) = std::fs::symlink_metadata(&path) {
            if metadata.file_type().is_symlink() {
                return Err(format!("更新路径包含链接：{name}"));
            }
        }
    }
    Ok(path)
}

fn hash_file(path: &Path) -> Result<String, String> {
    let mut file =
        std::fs::File::open(path).map_err(|e| format!("无法读取 {}：{e}", path.display()))?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let size = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        hash.update(&buffer[..size]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn verify_snapshot(files: &BTreeMap<String, String>) -> Result<(), String> {
    let mut seen = BTreeSet::new();
    for (name, hash) in files {
        relative_path(name)?;
        if !seen.insert(name.to_lowercase())
            || hash.len() != 64
            || !hash
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Err(format!("更新包文件清单无效：{name}"));
        }
    }
    if !files.contains_key("interface.json") || !files.contains_key("MaaNikki.exe") {
        return Err("更新包缺少程序文件清单".into());
    }
    Ok(())
}

fn collect_files(root: &Path, folder: &Path, result: &mut BTreeSet<String>) -> Result<(), String> {
    for entry in std::fs::read_dir(folder).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_symlink() {
            return Err("增量包包含链接".into());
        }
        if kind.is_dir() {
            collect_files(root, &entry.path(), result)?;
        } else {
            let path = entry.path();
            let name = path
                .strip_prefix(root)
                .map_err(|e| e.to_string())?
                .to_string_lossy()
                .replace('\\', "/");
            relative_path(&name)?;
            result.insert(name);
        }
    }
    Ok(())
}

fn check_version(folder: &Path, expected: &str) -> Result<(), String> {
    let text = std::fs::read_to_string(folder.join("interface.json")).map_err(|e| e.to_string())?;
    let interface: serde_json::Value =
        serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|e| e.to_string())?;
    if interface.get("version").and_then(|v| v.as_str()) != Some(expected) {
        return Err("本地版本或更新目标与增量包不匹配".into());
    }
    Ok(())
}

fn same_paths(actual: &[String], expected: &BTreeSet<String>) -> bool {
    actual.len() == expected.len() && actual.iter().cloned().collect::<BTreeSet<_>>() == *expected
}

fn remove_empty_tree(path: &Path) -> Result<(), String> {
    for entry in std::fs::read_dir(path).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        if !entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            return Err(format!("目录中仍有文件：{}", path.display()));
        }
        remove_empty_tree(&entry.path())?;
    }
    std::fs::remove_dir(path).map_err(|e| e.to_string())
}

fn apply(extract: &Path, target: &Path, new_version: &str) -> Result<(), String> {
    let text = std::fs::read_to_string(extract.join("changes.json")).map_err(|e| e.to_string())?;
    let patch: Patch = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    if patch.maanikki_patch != 1 || patch.to_version != new_version.trim_start_matches('v') {
        return Err("增量包格式或目标版本不匹配".into());
    }
    verify_snapshot(&patch.base_files)?;
    verify_snapshot(&patch.target_files)?;
    check_version(target, &patch.from_version)?;
    let before: BTreeSet<_> = patch.base_files.keys().cloned().collect();
    let after: BTreeSet<_> = patch.target_files.keys().cloned().collect();
    let added: BTreeSet<_> = after.difference(&before).cloned().collect();
    let deleted: BTreeSet<_> = before.difference(&after).cloned().collect();
    let modified: BTreeSet<_> = before
        .intersection(&after)
        .filter(|name| patch.base_files[*name] != patch.target_files[*name])
        .cloned()
        .collect();
    if !same_paths(&patch.added, &added)
        || !same_paths(&patch.deleted, &deleted)
        || !same_paths(&patch.modified, &modified)
        || !modified.contains("interface.json")
    {
        return Err("增量包变更清单不完整".into());
    }
    let changed: BTreeSet<_> = added.union(&modified).cloned().collect();
    let mut extracted = BTreeSet::new();
    collect_files(extract, extract, &mut extracted)?;
    extracted.remove("changes.json");
    if extracted != changed {
        return Err("增量包内容与文件清单不一致".into());
    }
    // All validation finishes before moving or overwriting any installed file.
    for (name, expected) in &patch.base_files {
        if hash_file(&checked_join(target, name)?)? != *expected {
            return Err(format!("本地文件已变化，改用完整更新：{name}"));
        }
    }
    for name in &changed {
        if hash_file(&checked_join(extract, name)?)? != patch.target_files[name] {
            return Err(format!("增量文件校验失败：{name}"));
        }
        let destination = checked_join(target, name)?;
        if added.contains(name) && destination.is_file() && !deleted.contains(name) {
            return Err(format!("新增文件与本地文件冲突：{name}"));
        }
    }
    check_version(extract, &patch.to_version)?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos();
    let backup = target.join("cache/old").join(format!("patch-{stamp}"));
    std::fs::create_dir_all(&backup).map_err(|e| e.to_string())?;
    let mut touched = Vec::new();
    let result = (|| {
        let mut writes: Vec<_> = changed
            .iter()
            .filter(|name| *name != "interface.json")
            .cloned()
            .collect();
        // Commit the project version only after all other files are installed.
        writes.push("interface.json".into());
        for name in deleted.iter().chain(writes.iter()) {
            let destination = checked_join(target, name)?;
            if destination.is_dir() {
                remove_empty_tree(&destination)?;
            }
            if destination.exists() {
                let saved = checked_join(&backup, name)?;
                std::fs::create_dir_all(saved.parent().unwrap()).map_err(|e| e.to_string())?;
                std::fs::rename(&destination, &saved)
                    .map_err(|e| format!("无法备份 {name}：{e}"))?;
            }
            touched.push(name.clone());
            if changed.contains(name) {
                std::fs::create_dir_all(destination.parent().unwrap())
                    .map_err(|e| e.to_string())?;
                std::fs::copy(checked_join(extract, name)?, &destination)
                    .map_err(|e| format!("无法更新 {name}：{e}"))?;
                if hash_file(&destination)? != patch.target_files[name] {
                    return Err(format!("更新后文件校验失败：{name}"));
                }
            }
        }
        Ok(())
    })();
    if let Err(error) = result {
        let mut rollback_errors = Vec::new();
        for name in touched.iter().rev() {
            let restore = (|| {
                let destination = checked_join(target, name)?;
                if destination.is_dir() {
                    remove_empty_tree(&destination)?;
                } else if destination.exists() {
                    std::fs::remove_file(&destination).map_err(|e| e.to_string())?;
                }
                let saved = checked_join(&backup, name)?;
                if saved.exists() {
                    std::fs::create_dir_all(destination.parent().unwrap())
                        .map_err(|e| e.to_string())?;
                    std::fs::rename(saved, destination).map_err(|e| e.to_string())?;
                }
                Ok::<_, String>(())
            })();
            if let Err(failure) = restore {
                rollback_errors.push(format!("{name}：{failure}"));
            }
        }
        if !rollback_errors.is_empty() {
            return Err(format!(
                "{error}；恢复旧文件失败：{}；备份位于 {}",
                rollback_errors.join("，"),
                backup.display()
            ));
        }
        return Err(error);
    }
    let _ = std::fs::remove_dir_all(&backup);
    log::info!(
        "增量更新完成：{} → {}，更新 {} 个文件，删除 {} 个文件",
        patch.from_version,
        patch.to_version,
        changed.len(),
        deleted.len()
    );
    Ok(())
}

#[tauri::command]
pub async fn apply_project_patch(
    extract_dir: String,
    target_dir: String,
    new_version: String,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let extract = std::fs::canonicalize(extract_dir).map_err(|e| e.to_string())?;
        let target = std::fs::canonicalize(target_dir).map_err(|e| e.to_string())?;
        apply(&extract, &target, &new_version)
    })
    .await
    .map_err(|e| e.to_string())?
}
