//! Infinity Nikki outfit share-code decoder (official public OSS CDN) and local collection storage.
use super::{data_update::resolve_data_file, utils::get_app_data_dir};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

const BASEN_CHARS: &[u8] = b"qT9aL4sXhR7wKdN2gFjZP5zYm8bEv1W0xU3oIc6tOrJukGpSfViHnDlBQMeyCA";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DyeColorEntry {
    pub group_id: i64,
    pub color_grid_id: i64,
    pub feature_tag: i64,
    pub r: f64,
    pub g: f64,
    pub b: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClothDyeSummary {
    pub cloth_id: u64,
    pub colors: Vec<DyeColorEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecodedShareCode {
    pub code: String,
    pub region: String,
    pub wearing_clothes: Vec<u64>,
    pub dye_summaries: Vec<ClothDyeSummary>,
    pub pattern_cloth_ids: Vec<u64>,
    pub hidden_fx_cloth_ids: Vec<u64>,
}

fn domain_for_prefix(prefix: char) -> Option<(&'static str, &'static str)> {
    match prefix {
        '1' => Some(("https://x6cn-clothdiydata.nuanpaper.com/default/", "国服")),
        '2' => Some((
            "https://x6cn-test-clothdiydata.nuanpaper.com/default/",
            "国服测试",
        )),
        '3' => Some((
            "https://x6cn-pre-clothdiydata.nuanpaper.com/default/",
            "国服预发",
        )),
        '4' => Some((
            "https://x6en-jp-clothdiydata.infoldgames.com/default/",
            "亚服",
        )),
        '5' => Some((
            "https://x6en-jp-test-clothdiydata.infoldgames.com/default/",
            "亚服测试",
        )),
        '6' => Some((
            "https://x6en-jp-pre-clothdiydata.infoldgames.com/default/",
            "亚服预发",
        )),
        '7' => Some((
            "https://x6en-vga-clothdiydata.infoldgames.com/default/",
            "美服",
        )),
        '8' => Some((
            "https://x6en-vga-test-clothdiydata.infoldgames.com/default/",
            "美服测试",
        )),
        '9' => Some((
            "https://x6en-vga-pre-clothdiydata.infoldgames.com/default/",
            "美服预发",
        )),
        'A' | 'K' => Some((
            "https://x6en-ffm-clothdiydata.infoldgames.com/default/",
            "欧服",
        )),
        'B' => Some((
            "https://x6en-ffm-test-clothdiydata.infoldgames.com/default/",
            "欧服测试",
        )),
        'C' => Some((
            "https://x6en-ffm-pre-clothdiydata.infoldgames.com/default/",
            "欧服预发",
        )),
        _ => None,
    }
}

/// Arbitrary-precision Base-N to base-10 decimal string matching ShareCodeModule.BaseNToDecimal.
fn base_n_to_decimal(encoded: &str) -> Result<String, String> {
    if encoded.is_empty() {
        return Err("搭配码主体为空。".into());
    }
    let base = BASEN_CHARS.len() as u32;
    let mut digits: Vec<u8> = vec![0]; // little-endian base-10 digits
    for byte in encoded.bytes() {
        let Some(pos) = BASEN_CHARS.iter().position(|&b| b == byte) else {
            return Err(format!("搭配码包含无效字符：{}", byte as char));
        };
        let mut carry = pos as u32;
        for d in digits.iter_mut() {
            let v = (*d as u32) * base + carry;
            *d = (v % 10) as u8;
            carry = v / 10;
        }
        while carry > 0 {
            digits.push((carry % 10) as u8);
            carry /= 10;
        }
    }
    while digits.len() > 1 && digits.last() == Some(&0) {
        digits.pop();
    }
    let mut out = String::with_capacity(digits.len());
    for d in digits.into_iter().rev() {
        out.push((b'0' + d) as char);
    }
    Ok(out)
}

/// Normalize Infold's custom Lua cjson integer-keyed tables (`[:1020:0,1021:1]`) into standard JSON.
fn sanitize_infold_cjson(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let len = bytes.len();
    let mut out = String::with_capacity(len + 64);
    let mut i = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    // Stack tracks whether each open `[` was converted from `[:` into `{`.
    let mut bracket_stack: Vec<bool> = Vec::new();

    while i < len {
        let b = bytes[i];
        if in_string {
            let ch = raw[i..].chars().next().unwrap();
            out.push(ch);
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            i += ch.len_utf8();
            continue;
        }
        if b == b'"' {
            in_string = true;
            out.push('"');
            i += 1;
            continue;
        }
        if b == b'{' {
            bracket_stack.push(false);
            out.push('{');
            i += 1;
            continue;
        }
        if b == b'}' {
            bracket_stack.pop();
            out.push('}');
            i += 1;
            continue;
        }
        if b == b'[' {
            if i + 1 < len && bytes[i + 1] == b':' {
                bracket_stack.push(true);
                out.push('{');
                i += 2;
                // Quote first numeric key if present immediately after `[:`.
                while i < len && bytes[i].is_ascii_whitespace() {
                    out.push(bytes[i] as char);
                    i += 1;
                }
                let start = i;
                if i < len && bytes[i] == b'-' {
                    i += 1;
                }
                while i < len && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                if i > start && i < len && bytes[i] == b':' {
                    out.push('"');
                    out.push_str(&raw[start..i]);
                    out.push('"');
                } else {
                    out.push_str(&raw[start..i]);
                }
                continue;
            }
            bracket_stack.push(false);
            out.push('[');
            i += 1;
            continue;
        }
        if b == b']' {
            let was_map = bracket_stack.pop().unwrap_or(false);
            out.push(if was_map { '}' } else { ']' });
            i += 1;
            continue;
        }
        if b == b',' && bracket_stack.last().copied() == Some(true) {
            out.push(',');
            i += 1;
            while i < len && bytes[i].is_ascii_whitespace() {
                out.push(bytes[i] as char);
                i += 1;
            }
            let start = i;
            if i < len && bytes[i] == b'-' {
                i += 1;
            }
            while i < len && bytes[i].is_ascii_digit() {
                i += 1;
            }
            if i > start && i < len && bytes[i] == b':' {
                out.push('"');
                out.push_str(&raw[start..i]);
                out.push('"');
            } else {
                out.push_str(&raw[start..i]);
            }
            continue;
        }
        // Preserve multi-byte UTF-8 characters safely.
        let ch = raw[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn parse_diy_payload(
    code: String,
    region: String,
    raw_body: &str,
) -> Result<DecodedShareCode, String> {
    let cleaned = sanitize_infold_cjson(raw_body);
    let root: Value =
        serde_json::from_str(&cleaned).map_err(|e| format!("搭配码数据解析失败：{}", e))?;

    // Unwrap nested {"Content": {"Content": {...}}} or {"Content": {...}} or direct object.
    let mut content = &root;
    for _ in 0..3 {
        if let Some(next) = content.get("Content").filter(|v| v.is_object()) {
            content = next;
        } else {
            break;
        }
    }

    let mut wearing_clothes: Vec<u64> = Vec::new();
    if let Some(arr) = content.get("wearingClothes").and_then(Value::as_array) {
        for item in arr {
            if let Some(id) = item.as_u64() {
                if id > 0 && !wearing_clothes.contains(&id) {
                    wearing_clothes.push(id);
                }
            }
        }
    }

    let mut dye_by_cloth: HashMap<u64, Vec<DyeColorEntry>> = HashMap::new();
    if let Some(arr) = content.get("wearingDIYInfos").and_then(Value::as_array) {
        for info in arr {
            let Some(cloth_id) = info.get("TargetClothID").and_then(Value::as_u64) else {
                continue;
            };
            let group_id = info
                .get("TargetGroupID")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            let feature_tag = info.get("FeatureTag").and_then(Value::as_i64).unwrap_or(0);
            let Some(core) = info.get("CoreData").and_then(Value::as_object) else {
                continue;
            };
            // Standard clothing dye: CoreData.{R, G, B, ColorGridID}
            if let (Some(r), Some(g), Some(b)) = (
                core.get("R").and_then(Value::as_f64),
                core.get("G").and_then(Value::as_f64),
                core.get("B").and_then(Value::as_f64),
            ) {
                let grid_id = core
                    .get("ColorGridID")
                    .and_then(Value::as_i64)
                    .unwrap_or(-1);
                dye_by_cloth
                    .entry(cloth_id)
                    .or_default()
                    .push(DyeColorEntry {
                        group_id,
                        color_grid_id: grid_id,
                        feature_tag,
                        r,
                        g,
                        b,
                    });
            }
            // Hair dye: CoreData.TargetColor0 / TargetColor1
            for (color_key, grid_key) in [
                ("TargetColor0", "ColorGridID0"),
                ("TargetColor1", "ColorGridID1"),
            ] {
                if let Some(tc) = core.get(color_key).and_then(Value::as_object) {
                    if let (Some(r), Some(g), Some(b)) = (
                        tc.get("R").and_then(Value::as_f64),
                        tc.get("G").and_then(Value::as_f64),
                        tc.get("B").and_then(Value::as_f64),
                    ) {
                        let grid_id = core.get(grid_key).and_then(Value::as_i64).unwrap_or(-1);
                        if grid_id != -1 || color_key == "TargetColor0" {
                            dye_by_cloth
                                .entry(cloth_id)
                                .or_default()
                                .push(DyeColorEntry {
                                    group_id,
                                    color_grid_id: grid_id,
                                    feature_tag,
                                    r,
                                    g,
                                    b,
                                });
                        }
                    }
                }
            }
            if !wearing_clothes.contains(&cloth_id) && cloth_id > 0 {
                wearing_clothes.push(cloth_id);
            }
        }
    }

    let mut pattern_cloth_ids: Vec<u64> = Vec::new();
    if let Some(map) = content.get("patternData").and_then(Value::as_object) {
        for (k, v) in map {
            if let (Ok(id), Some(flag)) = (k.parse::<u64>(), v.as_i64()) {
                if flag != 0 {
                    pattern_cloth_ids.push(id);
                }
            }
        }
    }

    let mut hidden_fx_cloth_ids: Vec<u64> = Vec::new();
    if let Some(map) = content.get("nsHiddenData").and_then(Value::as_object) {
        for (k, v) in map {
            if let (Ok(id), Some(flag)) = (k.parse::<u64>(), v.as_i64()) {
                if flag != 0 {
                    hidden_fx_cloth_ids.push(id);
                }
            }
        }
    }

    if wearing_clothes.is_empty() {
        return Err("搭配码内未包含任何服装部件数据。".into());
    }

    let mut dye_summaries: Vec<ClothDyeSummary> = dye_by_cloth
        .into_iter()
        .map(|(cloth_id, colors)| ClothDyeSummary { cloth_id, colors })
        .collect();
    dye_summaries.sort_by_key(|d| d.cloth_id);
    pattern_cloth_ids.sort_unstable();
    hidden_fx_cloth_ids.sort_unstable();

    Ok(DecodedShareCode {
        code,
        region,
        wearing_clothes,
        dye_summaries,
        pattern_cloth_ids,
        hidden_fx_cloth_ids,
    })
}

fn collection_path() -> Result<PathBuf, String> {
    Ok(get_app_data_dir()?.join("config/outfit_collection.json"))
}

#[tauri::command]
pub async fn decode_outfit_share_code(code: String) -> Result<DecodedShareCode, String> {
    let trimmed = code.trim();
    let core = trimmed.trim_end_matches('#').trim();
    if core.len() < 4 || core.len() > 32 {
        return Err("搭配码长度不正确，请检查是否完整复制。".into());
    }
    let mut chars = core.chars();
    let prefix = chars.next().ok_or("搭配码格式无效。")?;
    let body: String = chars.collect();
    let (base_url, region) = domain_for_prefix(prefix)
        .ok_or_else(|| format!("不支持的搭配码区服前缀「{}」。", prefix))?;

    let canonical = format!("{}#", core);
    let mut candidate_decimals = vec![base_n_to_decimal(&body)?];
    if body.contains('K') {
        if let Ok(alt) = base_n_to_decimal(&body.replace('K', "A")) {
            if !candidate_decimals.contains(&alt) {
                candidate_decimals.push(alt);
            }
        }
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(12))
        .user_agent("Mozilla/5.0")
        .build()
        .map_err(|e| format!("初始化网络请求失败：{}", e))?;

    let mut last_err = String::from("未能找到该搭配码，可能已失效或输入有误。");
    for dec in &candidate_decimals {
        let url = format!("{}{}.json", base_url, dec);
        match client.get(&url).send().await {
            Ok(resp) if resp.status().is_success() => {
                let text = resp
                    .text()
                    .await
                    .map_err(|e| format!("读取搭配码响应失败：{}", e))?;
                return parse_diy_payload(canonical, region.to_string(), &text);
            }
            Ok(resp) if resp.status().as_u16() == 404 => {
                last_err = "该搭配码不存在或已过期，请确认搭配码是否完整。".into();
            }
            Ok(resp) => {
                last_err = format!(
                    "官方搭配码服务返回异常状态（HTTP {}）。",
                    resp.status().as_u16()
                );
            }
            Err(err) => {
                last_err = if err.is_timeout() {
                    "连接搭配码服务超时，请稍后重试。".into()
                } else {
                    format!("请求搭配码失败：{}", err)
                };
            }
        }
    }
    Err(last_err)
}

#[tauri::command]
pub async fn get_outfit_collection() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let path = collection_path()?;
        if !path.is_file() {
            return Ok(json!({ "schema_version": 1, "items": [] }));
        }
        let raw = std::fs::read_to_string(&path).map_err(|e| format!("读取搭配收藏失败：{}", e))?;
        let val: Value =
            serde_json::from_str(&raw).map_err(|e| format!("搭配收藏文件格式损坏：{}", e))?;
        Ok(val)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn save_outfit_collection(collection: Value) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        if !collection.is_object() {
            return Err("搭配收藏数据格式无效。".into());
        }
        let path = collection_path()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("创建配置目录失败：{}", e))?;
        }
        let content = serde_json::to_string_pretty(&collection)
            .map_err(|e| format!("序列化搭配收藏失败：{}", e))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, content.as_bytes())
            .map_err(|e| format!("写入搭配收藏临时文件失败：{}", e))?;
        std::fs::rename(&tmp, &path).map_err(|e| format!("保存搭配收藏失败：{}", e))?;
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn get_outfit_item_dictionary() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let labels_path = resolve_data_file("resource/model/wardrobe/labels.json")?;
        let parts_path = resolve_data_file("resource/model/wardrobe/parts.json")?;
        let labels_raw = std::fs::read_to_string(&labels_path)
            .map_err(|e| format!("读取服装图鉴名称字典失败：{}", e))?;
        let parts_raw = std::fs::read_to_string(&parts_path)
            .map_err(|e| format!("读取部位名称字典失败：{}", e))?;
        let labels: Value = serde_json::from_str(&labels_raw)
            .map_err(|e| format!("解析服装图鉴名称字典失败：{}", e))?;
        let parts: Value =
            serde_json::from_str(&parts_raw).map_err(|e| format!("解析部位名称字典失败：{}", e))?;

        let mut part_names: Map<String, Value> = Map::new();
        if let Some(obj) = parts.as_object() {
            for (k, v) in obj {
                if let Some(name) = v.get("name").and_then(Value::as_str) {
                    part_names.insert(k.clone(), Value::String(name.to_string()));
                }
            }
        }

        let mut items: Map<String, Value> = Map::new();
        if let Some(obj) = labels.as_object() {
            for (part_code, rows) in obj {
                let Some(arr) = rows.as_array() else { continue };
                for row in arr {
                    let ids = row.get("item_ids").and_then(Value::as_array);
                    let names = row.get("names").and_then(Value::as_array);
                    let qualities = row.get("qualities").and_then(Value::as_array);
                    let Some(ids) = ids else { continue };
                    for (idx, id_val) in ids.iter().enumerate() {
                        let Some(id_num) = id_val.as_u64() else {
                            continue;
                        };
                        let name = names
                            .and_then(|a| a.get(idx).or_else(|| a.first()))
                            .and_then(Value::as_str)
                            .unwrap_or("");
                        let quality = qualities
                            .and_then(|a| a.get(idx).or_else(|| a.first()))
                            .and_then(Value::as_u64)
                            .unwrap_or(3);
                        items.insert(
                            id_num.to_string(),
                            json!({
                                "name": name,
                                "part": part_code,
                                "quality": quality,
                            }),
                        );
                    }
                }
            }
        }
        Ok(json!({
            "part_names": part_names,
            "items": items,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}
