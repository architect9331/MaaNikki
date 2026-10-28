//! Shared update mirror configuration and release checksums.
use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};

use serde::Deserialize;

use super::types::GitHubRelease;

#[derive(Deserialize)]
pub struct R2Mirror {
    pub public_url: String,
}

#[derive(Deserialize)]
pub struct MirrorConfig {
    pub repository: String,
    pub github_proxies: Vec<String>,
    pub sourceforge_project: Option<String>,
    pub r2: Option<R2Mirror>,
}

pub static CONFIG: LazyLock<MirrorConfig> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../../build/update-mirrors.json"))
        .expect("invalid update mirror configuration")
});

#[derive(Clone)]
pub struct AssetIntegrity {
    pub size: u64,
    pub sha256: String,
}

static INTEGRITY: LazyLock<Mutex<HashMap<String, AssetIntegrity>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub fn remember(releases: &[GitHubRelease]) {
    let mut cache = INTEGRITY.lock().unwrap_or_else(|e| e.into_inner());
    for release in releases.iter().filter(|release| !release.draft) {
        for asset in &release.assets {
            if let Some(hash) = asset
                .digest
                .as_deref()
                .and_then(|s| s.strip_prefix("sha256:"))
            {
                if hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit()) {
                    cache.insert(
                        asset.browser_download_url.clone(),
                        AssetIntegrity {
                            size: asset.size,
                            sha256: hash.to_ascii_lowercase(),
                        },
                    );
                }
            }
        }
    }
}

pub fn integrity(url: &str) -> Option<AssetIntegrity> {
    INTEGRITY
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(url)
        .cloned()
}

pub fn metadata_mirrors(api_url: &str) -> Vec<String> {
    let mut urls: Vec<_> = CONFIG
        .github_proxies
        .iter()
        .map(|base| format!("{}/{api_url}", base.trim_end_matches('/')))
        .collect();
    if let Some(project) = &CONFIG.sourceforge_project {
        urls.push(format!(
            "https://downloads.sourceforge.net/project/{project}/releases.json"
        ));
    }
    if let Some(r2) = &CONFIG.r2 {
        urls.push(format!(
            "{}/releases.json",
            r2.public_url.trim_end_matches('/')
        ));
    }
    urls
}

/// Only published assets of this repository may be sent to public mirrors.
pub fn release_asset(url: &str) -> Option<(String, String)> {
    let parsed = reqwest::Url::parse(url).ok()?;
    if parsed.scheme() != "https"
        || parsed.host_str() != Some("github.com")
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return None;
    }
    let prefix = format!("/{}/releases/download/", CONFIG.repository);
    let tail = parsed.path().strip_prefix(&prefix)?;
    let (tag, filename) = tail.split_once('/')?;
    if !tag.starts_with('v')
        || !filename.starts_with("MaaNikki-")
        || filename.contains('/')
        || ![tag, filename].iter().all(|s| {
            s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-._".contains(&b))
        })
    {
        return None;
    }
    Some((tag.into(), filename.into()))
}

pub fn download_mirrors(url: &str) -> Vec<String> {
    let Some((tag, filename)) = release_asset(url) else {
        return Vec::new();
    };
    let mut urls: Vec<_> = CONFIG
        .github_proxies
        .iter()
        .map(|base| format!("{}/{url}", base.trim_end_matches('/')))
        .collect();
    if let Some(project) = &CONFIG.sourceforge_project {
        urls.push(format!(
            "https://downloads.sourceforge.net/project/{project}/{tag}/{filename}"
        ));
    }
    if let Some(r2) = &CONFIG.r2 {
        urls.push(format!(
            "{}/{tag}/{filename}",
            r2.public_url.trim_end_matches('/')
        ));
    }
    urls
}
