//! Read-only PearPal open-world resource estimator (chests, whimstars, dewdrops).
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::utils::{get_app_data_dir, write_atomic};

const API: &str = "https://myl-api.nuanpaper.com";
const CLIENT_ID: u64 = 1106;
const PAYLOAD_LIMIT: usize = 32 * 1024 * 1024;
const CACHE_TTL_SECS: u64 = 24 * 3600;
const CACHE_SCHEMA: u32 = 1;

const WORLDS: [(&str, &str); 5] = [
    ("1", "miraland"),
    ("4020034", "wanxiang"),
    ("10000001", "firework"),
    ("10000002", "serenity"),
    ("10000010", "danqing"),
];

#[derive(Clone, Serialize, Deserialize)]
struct CachedPoint {
    id: String,
    catalog: String,
    group: String,
    subregion: String,
}

#[derive(Serialize, Deserialize)]
struct PublicResourceCache {
    schema_version: u32,
    fetched_at: String,
    points: Vec<CachedPoint>,
}

fn decode_pearpal_payload(raw: &[u8]) -> Result<Value, String> {
    let trimmed = raw
        .iter()
        .position(|b| !b.is_ascii_whitespace())
        .map(|pos| &raw[pos..])
        .unwrap_or(raw);
    if trimmed.starts_with(b"{") || trimmed.starts_with(b"[") {
        let val: Value = serde_json::from_slice(trimmed).map_err(|_| "美鸭梨数据解析失败。")?;
        return check_api_code(val);
    }

    let mut index = 0usize;
    let mut length = 0usize;
    let mut shift = 0u32;
    loop {
        if index >= raw.len() || shift > 28 {
            return Err("美鸭梨地图压缩数据无效。".into());
        }
        let byte = raw[index];
        index += 1;
        length |= ((byte & 127) as usize) << shift;
        if byte < 128 {
            break;
        }
        shift += 7;
    }
    if length > PAYLOAD_LIMIT {
        return Err("美鸭梨地图数据过大。".into());
    }

    let mut output = Vec::with_capacity(length.min(4 * 1024 * 1024));
    while index < raw.len() {
        let tag = raw[index];
        index += 1;
        let kind = tag & 3;
        let mut count = (tag >> 2) as usize;
        if kind == 0 {
            if count >= 60 {
                let size = count - 59;
                if index + size > raw.len() {
                    return Err("美鸭梨地图压缩数据不完整。".into());
                }
                let mut buf = [0u8; 4];
                buf[..size].copy_from_slice(&raw[index..index + size]);
                count = u32::from_le_bytes(buf) as usize;
                index += size;
            }
            count += 1;
            if index + count > raw.len() || output.len() + count > length {
                return Err("美鸭梨地图压缩数据不完整。".into());
            }
            output.extend_from_slice(&raw[index..index + count]);
            index += count;
        } else {
            let size = match kind {
                1 => 1usize,
                2 => 2usize,
                _ => 4usize,
            };
            if index + size > raw.len() {
                return Err("美鸭梨地图压缩数据不完整。".into());
            }
            let mut buf = [0u8; 4];
            buf[..size].copy_from_slice(&raw[index..index + size]);
            let mut offset = u32::from_le_bytes(buf) as usize;
            index += size;
            if kind == 1 {
                offset |= ((tag as usize) & 224) << 3;
                count = 4 + (((tag >> 2) & 7) as usize);
            } else {
                count += 1;
            }
            if offset == 0 || offset > output.len() || output.len() + count > length {
                return Err("美鸭梨地图压缩数据无效。".into());
            }
            for _ in 0..count {
                let byte = output[output.len() - offset];
                output.push(byte);
            }
        }
    }
    if output.len() != length {
        return Err("美鸭梨地图解压长度无效。".into());
    }
    let val: Value = serde_json::from_slice(&output).map_err(|_| "美鸭梨数据解析失败。")?;
    check_api_code(val)
}

fn check_api_code(value: Value) -> Result<Value, String> {
    let code = &value["code"];
    if !code.is_null() && code.as_i64() != Some(0) {
        return Err("美鸭梨接口未返回有效数据，请重新登录或稍后重试。".into());
    }
    Ok(value)
}

#[derive(Clone, Copy)]
enum ResourceKind {
    Star,
    Dew,
    BoxLow,
    BoxMid,
    BoxHigh,
    ChestSketch,
    ChestMomo,
}

fn classify_catalog(catalog: &str) -> Option<ResourceKind> {
    match catalog {
        "11" | "132" | "145" | "167" | "244" => Some(ResourceKind::Star),
        "12" | "133" | "146" | "168" | "245" => Some(ResourceKind::Dew),
        "13" | "152" => Some(ResourceKind::BoxLow),
        "14" | "151" => Some(ResourceKind::BoxMid),
        "15" | "17" | "147" => Some(ResourceKind::BoxHigh),
        "16" => Some(ResourceKind::ChestSketch),
        "259" => Some(ResourceKind::ChestMomo),
        _ => None,
    }
}

fn resolve_region(world_id: &str, tag: &str) -> (&'static str, &'static str) {
    match tag {
        "RookieMap" => ("wishfield", "rookie"),
        "Florawish" | "DreamWarehouse" | "WellOfFortune" => ("wishfield", "florawish"),
        "BreezyMeadow" | "QueensRuins" | "SerpentineRuins" => ("wishfield", "breezy"),
        "Stonewoods" | "RippleWineCellar" | "HauntedTrain" => ("wishfield", "stonewoods"),
        "WishingWoods" | "TimisBeautyLab" | "RadiantSwordsmith" => ("wishfield", "wishing"),
        "GiantWoods" => ("yizan", "giant_woods"),
        "SnailCity" => ("yizan", "snail_city"),
        "GiantsCity" | "RestingGrounds" | "ItzaCanyon" | "ColossusColosseum" | "DawdlePark"
        | "MemoryOfMei" | "MushroomBank" | "MushroomCave" | "SpiritualTide" | "SweetFantasy" => {
            ("yizan", "yizan_other")
        }
        "EM_01_01" => ("firework", "firework"),
        "EM_02_01" => ("serenity", "serenity"),
        "EM_10_01" => ("danqing", "danqing"),
        "EM_34_01" => ("wanxiang", "wanxiang"),
        _ => match world_id {
            "10000001" => ("firework", "firework"),
            "10000002" => ("serenity", "serenity"),
            "10000010" => ("danqing", "danqing"),
            "4020034" => ("wanxiang", "wanxiang"),
            _ => ("yizan", "yizan_other"),
        },
    }
}

fn extract_catalog_str(item: &Value) -> Option<String> {
    let raw = item
        .get("catalog")
        .or_else(|| item.get("catalog_id"))
        .or_else(|| item.get("catalogId"))
        .or_else(|| item.get("category"))
        .or_else(|| {
            item.get("catalog_ids")
                .and_then(Value::as_array)
                .filter(|arr| arr.len() == 1)
                .and_then(|arr| arr.first())
        })?;
    let val = if let Some(arr) = raw.as_array() {
        arr.first()?
    } else {
        raw
    };
    if let Some(obj) = val.as_object() {
        return obj.get("id").and_then(value_to_id);
    }
    value_to_id(val)
}

fn value_to_id(val: &Value) -> Option<String> {
    if let Some(s) = val.as_str() {
        let trimmed = s.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    } else if let Some(n) = val.as_u64() {
        return Some(n.to_string());
    } else if let Some(n) = val.as_i64() {
        return Some(n.to_string());
    }
    None
}

fn parse_id_set(val: &Value) -> HashSet<String> {
    let mut set = HashSet::new();
    let parsed_str;
    let target = if let Some(s) = val.as_str() {
        if let Ok(v) = serde_json::from_str::<Value>(s) {
            parsed_str = v;
            &parsed_str
        } else {
            return set;
        }
    } else {
        val
    };
    if let Some(arr) = target.as_array() {
        for item in arr {
            if let Some(id) = value_to_id(item) {
                set.insert(id);
            }
        }
    }
    set
}

fn cache_file_path() -> Result<PathBuf, String> {
    Ok(get_app_data_dir()?.join("cache/map-mask/resource_estimator_v1.json"))
}

fn is_cache_fresh(path: &PathBuf) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    let Ok(modified) = meta.modified() else {
        return false;
    };
    SystemTime::now()
        .duration_since(modified)
        .map(|d| d.as_secs() < CACHE_TTL_SECS)
        .unwrap_or(false)
}

async fn post_map_api(client: &reqwest::Client, path: &str, body: Value) -> Result<Value, String> {
    let resp = client
        .post(format!("{API}/v1/strategy/map/{path}"))
        .header("User-Agent", "MaaNikki-Map/1.0")
        .json(&body)
        .send()
        .await
        .map_err(|_| "美鸭梨地图服务连接失败，请稍后重试。".to_string())?
        .error_for_status()
        .map_err(|_| "美鸭梨地图服务暂不可用。".to_string())?;
    let bytes = resp
        .bytes()
        .await
        .map_err(|_| "美鸭梨地图数据读取失败。".to_string())?;
    if bytes.len() > PAYLOAD_LIMIT {
        return Err("美鸭梨地图数据过大。".into());
    }
    decode_pearpal_payload(&bytes)
}

async fn load_or_refresh_public_cache(
    client: &reqwest::Client,
    force_refresh: bool,
) -> Result<PublicResourceCache, String> {
    let path = cache_file_path()?;
    if !force_refresh && is_cache_fresh(&path) {
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if let Ok(parsed) = serde_json::from_str::<PublicResourceCache>(&raw) {
                if parsed.schema_version == CACHE_SCHEMA && !parsed.points.is_empty() {
                    return Ok(parsed);
                }
            }
        }
    }

    let mut points_by_id: BTreeMap<String, CachedPoint> = BTreeMap::new();
    for (world_id, _) in WORLDS {
        let res = post_map_api(
            client,
            "spawner/list",
            json!({ "client_id": CLIENT_ID, "world_id": world_id, "catalog_type_id": [] }),
        )
        .await?;
        let Some(list) = res["list"].as_array() else {
            continue;
        };
        for item in list {
            let Some(id) = item.get("id").and_then(value_to_id) else {
                continue;
            };
            let Some(cat) = extract_catalog_str(item) else {
                continue;
            };
            if classify_catalog(&cat).is_none() {
                continue;
            }
            let tag = item["tag"].as_str().unwrap_or("");
            let (group, subregion) = resolve_region(world_id, tag);
            points_by_id.insert(
                id.clone(),
                CachedPoint {
                    id,
                    catalog: cat,
                    group: group.to_string(),
                    subregion: subregion.to_string(),
                },
            );
        }
    }

    let stages_res = post_map_api(
        client,
        "stage/spawner/list",
        json!({ "client_id": CLIENT_ID }),
    )
    .await?;
    if let Some(stages) = stages_res["stages"].as_object() {
        for (sid, children) in stages {
            let Some(arr) = children.as_array() else {
                continue;
            };
            for item in arr {
                let Some(id) = item.get("id").and_then(value_to_id) else {
                    continue;
                };
                if points_by_id.contains_key(&id) {
                    continue;
                }
                let Some(cat) = extract_catalog_str(item) else {
                    continue;
                };
                if classify_catalog(&cat).is_none() {
                    continue;
                }
                let tag = item["tag"].as_str().unwrap_or("");
                let world_hint = item["world_id"]
                    .as_str()
                    .or_else(|| item["worldId"].as_str())
                    .unwrap_or(sid.as_str());
                let (group, subregion) = resolve_region(world_hint, tag);
                points_by_id.insert(
                    id.clone(),
                    CachedPoint {
                        id,
                        catalog: cat,
                        group: group.to_string(),
                        subregion: subregion.to_string(),
                    },
                );
            }
        }
    }

    let cache = PublicResourceCache {
        schema_version: CACHE_SCHEMA,
        fetched_at: Utc::now().to_rfc3339(),
        points: points_by_id.into_values().collect(),
    };
    if let Ok(encoded) = serde_json::to_vec(&cache) {
        let _ = write_atomic(&path, &encoded);
    }
    Ok(cache)
}

#[derive(Default)]
struct BucketCounts {
    star_total: u64,
    star_collected: u64,
    dew_total: u64,
    dew_collected: u64,
    box_low_total: u64,
    box_low_rem: u64,
    box_mid_total: u64,
    box_mid_rem: u64,
    box_high_total: u64,
    box_high_rem: u64,
    chest_sketch_total: u64,
    chest_sketch_rem: u64,
    chest_momo_total: u64,
    chest_momo_rem: u64,
}

impl BucketCounts {
    fn add_point(&mut self, kind: ResourceKind, collected: bool) {
        match kind {
            ResourceKind::Star => {
                self.star_total += 1;
                if collected {
                    self.star_collected += 1;
                }
            }
            ResourceKind::Dew => {
                self.dew_total += 1;
                if collected {
                    self.dew_collected += 1;
                }
            }
            ResourceKind::BoxLow => {
                self.box_low_total += 1;
                if !collected {
                    self.box_low_rem += 1;
                }
            }
            ResourceKind::BoxMid => {
                self.box_mid_total += 1;
                if !collected {
                    self.box_mid_rem += 1;
                }
            }
            ResourceKind::BoxHigh => {
                self.box_high_total += 1;
                if !collected {
                    self.box_high_rem += 1;
                }
            }
            ResourceKind::ChestSketch => {
                self.chest_sketch_total += 1;
                if !collected {
                    self.chest_sketch_rem += 1;
                }
            }
            ResourceKind::ChestMomo => {
                self.chest_momo_total += 1;
                if !collected {
                    self.chest_momo_rem += 1;
                }
            }
        }
    }

    fn box_total(&self) -> u64 {
        self.box_low_total
            + self.box_mid_total
            + self.box_high_total
            + self.chest_sketch_total
            + self.chest_momo_total
    }

    fn box_remaining(&self) -> u64 {
        self.box_low_rem
            + self.box_mid_rem
            + self.box_high_rem
            + self.chest_sketch_rem
            + self.chest_momo_rem
    }

    fn box_remaining_diamonds(&self) -> u64 {
        self.box_low_rem * 5
            + self.box_mid_rem * 10
            + self.box_high_rem * 30
            + self.chest_sketch_rem * 5
    }

    fn box_json(&self) -> Value {
        let total = self.box_total();
        let remaining = self.box_remaining();
        json!({
            "total": total,
            "collected": total.saturating_sub(remaining),
            "remaining": remaining,
            "remaining_diamonds": self.box_remaining_diamonds(),
            "tiers_remaining": {
                "low": self.box_low_rem,
                "mid": self.box_mid_rem,
                "high": self.box_high_rem,
                "sketch": self.chest_sketch_rem,
                "momo": self.chest_momo_rem,
            }
        })
    }
}

struct GroupMeta {
    id: &'static str,
    label: &'static str,
    star_label: &'static str,
    star_cap_diamonds: u64,
    star_cap_standard: u64,
    dew_label: &'static str,
    dew_cap_diamonds: u64,
    dew_cap_standard: u64,
    rough_chest_tiers: bool,
    subregions: &'static [(&'static str, &'static str)],
}

const GROUPS: [GroupMeta; 6] = [
    GroupMeta {
        id: "wishfield",
        label: "心愿原野",
        star_label: "奇想星",
        star_cap_diamonds: 0,
        star_cap_standard: 0,
        dew_label: "灵感露珠",
        dew_cap_diamonds: 450,
        dew_cap_standard: 10,
        rough_chest_tiers: false,
        subregions: &[
            ("rookie", "纪念山地"),
            ("florawish", "花愿镇"),
            ("breezy", "微风绿野"),
            ("stonewoods", "石树田（小石树田村·无人区）"),
            ("wishing", "祈愿树林"),
        ],
    },
    GroupMeta {
        id: "yizan",
        label: "伊赞之土",
        star_label: "奇想星",
        star_cap_diamonds: 0,
        star_cap_standard: 0,
        dew_label: "灵感露珠",
        dew_cap_diamonds: 220,
        dew_cap_standard: 6,
        rough_chest_tiers: true,
        subregions: &[
            ("giant_woods", "巨木之森"),
            ("snail_city", "蜗牛城"),
            ("yizan_other", "巨兽堡垒·埋骨地及其他"),
        ],
    },
    GroupMeta {
        id: "firework",
        label: "花焰群岛",
        star_label: "奇想气球",
        star_cap_diamonds: 160,
        star_cap_standard: 0,
        dew_label: "焰光结晶",
        dew_cap_diamonds: 0,
        dew_cap_standard: 8,
        rough_chest_tiers: false,
        subregions: &[],
    },
    GroupMeta {
        id: "serenity",
        label: "无忧岛",
        star_label: "奇想气泡",
        star_cap_diamonds: 200,
        star_cap_standard: 0,
        dew_label: "无忧凝珠",
        dew_cap_diamonds: 0,
        dew_cap_standard: 8,
        rough_chest_tiers: false,
        subregions: &[],
    },
    GroupMeta {
        id: "danqing",
        label: "丹青屿",
        star_label: "奇想玉琚",
        star_cap_diamonds: 200,
        star_cap_standard: 5,
        dew_label: "丹青玉魄",
        dew_cap_diamonds: 0,
        dew_cap_standard: 10,
        rough_chest_tiers: false,
        subregions: &[],
    },
    GroupMeta {
        id: "wanxiang",
        label: "万相境",
        star_label: "奇想华灯",
        star_cap_diamonds: 200,
        star_cap_standard: 0,
        dew_label: "万相金铃",
        dew_cap_diamonds: 0,
        dew_cap_standard: 8,
        rough_chest_tiers: true,
        subregions: &[],
    },
];

fn estimate_milestone(
    collected: u64,
    total: u64,
    cap_diamonds: u64,
    cap_standard: u64,
) -> (u64, u64) {
    let remaining = total.saturating_sub(collected);
    if total == 0 || remaining == 0 {
        return (0, 0);
    }
    let ratio = (remaining as f64) / (total as f64);
    let est_diamonds = ((cap_diamonds as f64) * ratio).round() as u64;
    let est_standard = ((cap_standard as f64) * ratio).round() as u64;
    (
        est_diamonds.min(cap_diamonds),
        est_standard.min(cap_standard),
    )
}

pub async fn get_map_resources_impl(force_refresh: bool) -> Result<Value, String> {
    let credentials = match super::pearpal::credentials() {
        Ok(creds) => creds,
        Err(msg) => {
            return Ok(json!({
                "authenticated": false,
                "has_login": false,
                "error": msg,
            }));
        }
    };
    let (Some(token), Some(openid)) = (
        credentials["token"].as_str(),
        credentials["openid"].as_str(),
    ) else {
        return Ok(json!({
            "authenticated": false,
            "has_login": false,
            "error": "美鸭梨登录信息无效，请重新登录。",
        }));
    };

    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|_| "无法初始化网络客户端。".to_string())?;

    let user_res = client
        .post(format!("{API}/v1/strategy/map/user/info"))
        .header("User-Agent", "MaaNikki-Map/1.0")
        .json(&json!({ "client_id": CLIENT_ID, "token": token, "openid": openid }))
        .send()
        .await
        .map_err(|_| "美鸭梨连接失败，请稍后重试。".to_string())?
        .error_for_status()
        .map_err(|_| "美鸭梨服务暂不可用。".to_string())?
        .json::<Value>()
        .await
        .map_err(|_| "美鸭梨用户数据格式无效。".to_string())?;

    if user_res["code"] != 0 || !user_res["data"]["star"].is_array() {
        return Ok(json!({
            "authenticated": false,
            "has_login": true,
            "error": "美鸭梨登录已过期，请重新登录。",
        }));
    }

    let user_data = &user_res["data"];
    let star_set = parse_id_set(&user_data["star"]);
    let dew_set = parse_id_set(&user_data["dewdrop"]);
    let box_val = if user_data["box"].is_null() {
        &user_data["chest"]
    } else {
        &user_data["box"]
    };
    let box_set = parse_id_set(box_val);

    if let Ok(app_dir) = get_app_data_dir() {
        let status_path = app_dir.join("config/map-mask/status.json");
        let status_doc = json!({
            "authenticated": true,
            "error": "",
            "star_collected": star_set.len(),
            "dewdrop_collected": dew_set.len(),
            "box_collected": box_set.len(),
            "updated_at": Utc::now().timestamp(),
        });
        let _ = write_atomic(&status_path, status_doc.to_string());
    }

    let cache = load_or_refresh_public_cache(&client, force_refresh).await?;
    let mut group_buckets: HashMap<String, BucketCounts> = HashMap::new();
    let mut sub_buckets: HashMap<(String, String), BucketCounts> = HashMap::new();

    for pt in &cache.points {
        let Some(kind) = classify_catalog(&pt.catalog) else {
            continue;
        };
        let collected = match kind {
            ResourceKind::Star => star_set.contains(&pt.id),
            ResourceKind::Dew => dew_set.contains(&pt.id),
            _ => box_set.contains(&pt.id),
        };
        group_buckets
            .entry(pt.group.clone())
            .or_default()
            .add_point(kind, collected);
        sub_buckets
            .entry((pt.group.clone(), pt.subregion.clone()))
            .or_default()
            .add_point(kind, collected);
    }

    let mut total_box_diamonds = 0u64;
    let mut total_milestone_diamonds = 0u64;
    let mut total_milestone_standard = 0u64;
    let mut total_stars = 0u64;
    let mut collected_stars = 0u64;
    let mut total_dews = 0u64;
    let mut collected_dews = 0u64;
    let mut total_boxes = 0u64;
    let mut collected_boxes = 0u64;
    let mut groups_json = Vec::new();

    for spec in GROUPS {
        let bucket = group_buckets.remove(spec.id).unwrap_or_default();
        let box_diamonds = bucket.box_remaining_diamonds();
        let (star_est_d, star_est_s) = estimate_milestone(
            bucket.star_collected,
            bucket.star_total,
            spec.star_cap_diamonds,
            spec.star_cap_standard,
        );
        let (dew_est_d, dew_est_s) = estimate_milestone(
            bucket.dew_collected,
            bucket.dew_total,
            spec.dew_cap_diamonds,
            spec.dew_cap_standard,
        );

        total_box_diamonds += box_diamonds;
        total_milestone_diamonds += star_est_d + dew_est_d;
        total_milestone_standard += star_est_s + dew_est_s;
        total_stars += bucket.star_total;
        collected_stars += bucket.star_collected;
        total_dews += bucket.dew_total;
        collected_dews += bucket.dew_collected;
        total_boxes += bucket.box_total();
        collected_boxes += bucket.box_total().saturating_sub(bucket.box_remaining());

        let mut subs_json = Vec::new();
        for &(sub_id, sub_label) in spec.subregions {
            let sub = sub_buckets
                .remove(&(spec.id.to_string(), sub_id.to_string()))
                .unwrap_or_default();
            subs_json.push(json!({
                "id": sub_id,
                "label": sub_label,
                "boxes": sub.box_json(),
                "stars": {
                    "total": sub.star_total,
                    "collected": sub.star_collected,
                    "remaining": sub.star_total.saturating_sub(sub.star_collected),
                },
                "dewdrops": {
                    "total": sub.dew_total,
                    "collected": sub.dew_collected,
                    "remaining": sub.dew_total.saturating_sub(sub.dew_collected),
                },
            }));
        }

        groups_json.push(json!({
            "id": spec.id,
            "label": spec.label,
            "rough_chest_tiers": spec.rough_chest_tiers,
            "boxes": bucket.box_json(),
            "stars": {
                "label": spec.star_label,
                "total": bucket.star_total,
                "collected": bucket.star_collected,
                "remaining": bucket.star_total.saturating_sub(bucket.star_collected),
                "cap_diamonds": spec.star_cap_diamonds,
                "cap_standard_pulls": spec.star_cap_standard,
                "est_remaining_diamonds": star_est_d,
                "est_remaining_standard_pulls": star_est_s,
            },
            "dewdrops": {
                "label": spec.dew_label,
                "total": bucket.dew_total,
                "collected": bucket.dew_collected,
                "remaining": bucket.dew_total.saturating_sub(bucket.dew_collected),
                "cap_diamonds": spec.dew_cap_diamonds,
                "cap_standard_pulls": spec.dew_cap_standard,
                "est_remaining_diamonds": dew_est_d,
                "est_remaining_standard_pulls": dew_est_s,
            },
            "est_total_diamonds": box_diamonds + star_est_d + dew_est_d,
            "est_total_standard_pulls": star_est_s + dew_est_s,
            "subregions": subs_json,
        }));
    }

    let total_est_diamonds = total_box_diamonds + total_milestone_diamonds;
    Ok(json!({
        "authenticated": true,
        "has_login": true,
        "cache_fetched_at": cache.fetched_at,
        "synced_at": Utc::now().to_rfc3339(),
        "totals": {
            "box_remaining_diamonds": total_box_diamonds,
            "milestone_est_diamonds": total_milestone_diamonds,
            "milestone_est_standard_pulls": total_milestone_standard,
            "total_est_diamonds": total_est_diamonds,
            "approx_diamond_pulls": total_est_diamonds / 120,
            "stars": {
                "total": total_stars,
                "collected": collected_stars,
                "remaining": total_stars.saturating_sub(collected_stars),
            },
            "dewdrops": {
                "total": total_dews,
                "collected": collected_dews,
                "remaining": total_dews.saturating_sub(collected_dews),
            },
            "boxes": {
                "total": total_boxes,
                "collected": collected_boxes,
                "remaining": total_boxes.saturating_sub(collected_boxes),
            },
        },
        "groups": groups_json,
        "untracked_categories": [
            { "label": "见闻任务", "theory": "约 3,520 钻" },
            { "label": "风物逸事", "theory": "约 4,615 钻 + 2 限定 + 17 常驻" },
            { "label": "小游戏秘境", "theory": "约 4,715 钻 + 1 常驻" },
            { "label": "民俗游戏（含火锅坊）", "theory": "约 1,670 钻 + 4 常驻" },
            { "label": "搭配挑战", "theory": "约 3,540 钻 + 4 常驻" },
            { "label": "流转之柱", "theory": "约 3,620 钻" },
        ],
    }))
}

#[tauri::command]
pub async fn pearpal_map_resources(force_refresh: Option<bool>) -> Result<Value, String> {
    get_map_resources_impl(force_refresh.unwrap_or(false)).await
}
