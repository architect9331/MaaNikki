//! Optional screenshot storage, independent of Sentry's attachment transport.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use reqwest::{redirect::Policy, Url};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use super::task_diagnostics::ImageBundle;

static ACTIVE_UPLOADS: AtomicUsize = AtomicUsize::new(0);

struct UploadSlot;

impl Drop for UploadSlot {
    fn drop(&mut self) {
        ACTIVE_UPLOADS.fetch_sub(1, Ordering::SeqCst);
    }
}

#[derive(Deserialize)]
struct Receipt {
    id: String,
    bytes: u64,
    sha256: String,
    expires_at: u64,
}

pub(super) struct UploadedBundle {
    pub bundle: ImageBundle,
    pub url: String,
    pub bytes: u64,
    pub sha256: String,
    pub expires_at: u64,
}

/// Runs only on the existing evidence worker. Polling cancellation drops the HTTP
/// future (including an in-flight body) when consent is revoked or the app exits.
pub(super) fn upload(
    endpoint: &str,
    mut bundle: ImageBundle,
    allowed: impl Fn() -> bool,
) -> Result<UploadedBundle, String> {
    if !allowed() {
        return Err("diagnostic upload cancelled".into());
    }
    ACTIVE_UPLOADS
        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |count| {
            (count < 2).then_some(count + 1)
        })
        .map_err(|_| "diagnostic upload concurrency limit reached".to_string())?;
    let _slot = UploadSlot;
    let endpoint = Url::parse(endpoint).map_err(|_| "invalid screenshot endpoint".to_string())?;
    if endpoint.scheme() != "https"
        || endpoint.host_str().is_none()
        || !endpoint.username().is_empty()
        || endpoint.password().is_some()
        || endpoint.query().is_some()
        || endpoint.fragment().is_some()
        || !endpoint.path().ends_with("/upload")
    {
        return Err("screenshot endpoint must be an HTTPS upload URL".into());
    }
    let bytes = bundle.buffer.len() as u64;
    let sha256 = format!("{:x}", Sha256::digest(&bundle.buffer));
    let data = std::mem::take(&mut bundle.buffer);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| "diagnostic network runtime unavailable".to_string())?;
    let receipt = runtime.block_on(async {
        let client = reqwest::Client::builder()
            .redirect(Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(20))
            .build()
            .map_err(|_| "diagnostic HTTP client unavailable".to_string())?;
        let request = async {
            let mut response = client
                .post(endpoint.clone())
                .header(reqwest::header::CONTENT_TYPE, "application/zip")
                .body(data)
                .send()
                .await
                .map_err(|_| "screenshot upload network failure".to_string())?;
            if response.status() != reqwest::StatusCode::CREATED {
                return Err(format!(
                    "screenshot storage returned HTTP {}",
                    response.status()
                ));
            }
            let mut body = Vec::new();
            while let Some(chunk) = response
                .chunk()
                .await
                .map_err(|_| "screenshot receipt read failed".to_string())?
            {
                if body.len() + chunk.len() > 4096 {
                    return Err("screenshot receipt exceeded size limit".to_string());
                }
                body.extend_from_slice(&chunk);
            }
            serde_json::from_slice::<Receipt>(&body)
                .map_err(|_| "invalid screenshot receipt".to_string())
        };
        tokio::pin!(request);
        loop {
            if !allowed() {
                return Err("diagnostic upload cancelled".to_string());
            }
            if let Ok(result) =
                tokio::time::timeout(Duration::from_millis(100), request.as_mut()).await
            {
                return result;
            }
        }
    })?;
    if receipt.bytes != bytes
        || receipt.sha256 != sha256
        || receipt.id.len() != 64
        || !receipt
            .id
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err("screenshot receipt did not match uploaded bundle".into());
    }
    // Construct the download URL locally; the server cannot redirect Sentry users
    // to another origin or insert credentials into an event.
    let url = endpoint
        .join(&format!("files/{}.zip", receipt.id))
        .map_err(|_| "invalid screenshot download URL".to_string())?;
    Ok(UploadedBundle {
        bundle,
        url: url.into(),
        bytes,
        sha256,
        expires_at: receipt.expires_at,
    })
}
