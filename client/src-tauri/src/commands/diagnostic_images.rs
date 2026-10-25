//! Apply the shared small UID mask to native MaaFramework error screenshots.
use std::io::Cursor;

use image::{DynamicImage, ImageFormat, Rgba};
use serde::Deserialize;

#[derive(Deserialize)]
struct Policy {
    reference_size: [u32; 2],
    uid_roi: [u32; 4],
}

pub(super) fn sanitize_png(bytes: &[u8]) -> Result<Vec<u8>, String> {
    let mut image = image::load_from_memory_with_format(bytes, ImageFormat::Png)
        .map_err(|error| error.to_string())?
        .to_rgba8();
    let (width, height) = image.dimensions();
    // Native captures use 720p; support full HD reference captures too. Never
    // infer a UID corner in card crops or older screenshots with a trimmed bottom.
    if width < 960 || !matches!(height, 720 | 1080 | 1440 | 2160) {
        return Ok(bytes.to_vec());
    }
    let policy: Policy = serde_json::from_str(include_str!(
        "../../../../resource/data/diagnostic_images.json"
    ))
    .map_err(|error| error.to_string())?;
    let [x, y, w, h] = policy.uid_roi;
    let scale = |value: u32| {
        ((u64::from(value) * u64::from(height) + u64::from(policy.reference_size[1]) / 2)
            / u64::from(policy.reference_size[1])) as u32
    };
    for py in scale(y).min(height)..scale(y + h).min(height) {
        for px in scale(x).min(width)..scale(x + w).min(width) {
            image.put_pixel(px, py, Rgba([0, 0, 0, 255]));
        }
    }
    let mut encoded = Cursor::new(Vec::new());
    DynamicImage::ImageRgba8(image)
        .write_to(&mut encoded, ImageFormat::Png)
        .map_err(|error| error.to_string())?;
    Ok(encoded.into_inner())
}
