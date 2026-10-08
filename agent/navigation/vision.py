"""Map correlation and camera-cone boundaries; screenshot-only Maa adapter."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
import cv2
import numpy as np
from scipy.signal import find_peaks
from .models import MapSpec, NavigationError


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    score: float
    margin: float
    heading: float | None
    heading_confidence: float

    def dict(self):
        return asdict(self)


def luma(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)[:, :, 0] if frame.ndim == 3 else frame


def circular_smooth(values, radius):
    return sum(np.roll(values, n)*(radius-abs(n))//radius for n in range(1-radius, radius))


def prominence(values):
    length = values.size
    peaks, info = find_peaks(np.tile(values, 3), height=0, prominence=10)
    heights = sorted((height for p, height in zip(peaks, info["peak_heights"]) if length <= p < 2*length), reverse=True)
    return float((heights[0]-heights[1])/heights[0]) if len(heights) > 1 and heights[0] else 1.0


class Locator:
    def __init__(self, spec: MapSpec):
        self.spec = spec
        if not spec.image.is_file():
            raise NavigationError("地图资源缺失。")
        self.map = cv2.imread(str(spec.image), cv2.IMREAD_GRAYSCALE)
        if self.map is None or self.map.shape != (spec.height, spec.width):
            raise NavigationError("地图图片尺寸不符合清单。")
        coarse = spec.image.with_name(spec.id+"_bigmap.png")
        mask = spec.image.with_name(spec.id+"_mask.png")
        self.coarse = cv2.imread(str(coarse), cv2.IMREAD_GRAYSCALE) if coarse.is_file() else cv2.resize(self.map, None, fx=.25, fy=.25)
        self.bigmap_mask = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE) if mask.is_file() else None
        self.previous = None
        self.position_time = time.monotonic()-30
        self.heading_diagnostics = {}
        self.last_frame = None

    @staticmethod
    def match(image, patch, mask=None, peak_mask=None, kernel=5):
        if image.shape[0] < patch.shape[0] or image.shape[1] < patch.shape[1]:
            raise NavigationError("地图匹配区域不足。")
        rates = cv2.matchTemplate(image, patch, cv2.TM_CCOEFF_NORMED, mask=mask)
        rates = np.nan_to_num(rates, nan=-1, posinf=-1, neginf=-1)
        local = rates-cv2.GaussianBlur(rates, (kernel, kernel), 0)
        if peak_mask is not None:
            h, w = local.shape
            # Stored masks describe big-map correlation centers. A mini-map
            # patch is smaller and produces a larger response grid; extend
            # the mask with forbidden border centers instead of resizing it
            # or rejecting an otherwise valid global mini-map lookup.
            mh, mw = peak_mask.shape
            dh, dw = max(0, h-mh), max(0, w-mw)
            if dh or dw:
                peak_mask = cv2.copyMakeBorder(peak_mask, dh//2, dh-dh//2, dw//2, dw-dw//2,
                                               cv2.BORDER_CONSTANT, value=0)
            oy, ox = (peak_mask.shape[0]-h)//2, (peak_mask.shape[1]-w)//2
            local = np.where(peak_mask[oy:oy+h, ox:ox+w] > 0, local, -np.inf)
        _, detail, _, (px, py) = cv2.minMaxLoc(local)
        left, top = max(0, px-4), max(0, py-4)
        fine = rates[top:min(rates.shape[0], py+4), left:min(rates.shape[1], px+4)]
        if not fine.size or not np.isfinite(detail):
            raise NavigationError("没有可识别的地图区域。")
        _, score, _, (fx, fy) = cv2.minMaxLoc(cv2.resize(fine, None, fx=20, fy=20, interpolation=cv2.INTER_CUBIC))
        return left+fx/20-1+patch.shape[1]/2, top+fy/20-1+patch.shape[0]/2, float(score), float(detail)

    def _frame(self, frame):
        if frame is None or frame.shape[:2] != (720, 1280):
            raise NavigationError("导航需要 16:9 游戏画面，截图基准为 1280×720。")
        return cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)

    def locate(self, frame, hint=None, search_radius=75):
        self.last_frame = frame
        reference = self._frame(frame)
        gray = luma(reference[22:222, 81:281])
        yy, xx = np.indices(gray.shape)
        ring = ((xx-100)**2+(yy-100)**2 <= 100**2) & ((xx-100)**2+(yy-100)**2 > 13**2)
        scale = self.spec.scale/1.5
        patch = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        mask = cv2.resize(ring.astype(np.uint8)*255, (patch.shape[1], patch.shape[0]), interpolation=cv2.INTER_NEAREST)
        if hint is None and self.previous:
            hint = self.previous.x, self.previous.y
        if hint is None:
            small = cv2.resize(patch, None, fx=.25, fy=.25, interpolation=cv2.INTER_NEAREST)
            px, py, _, _ = self.match(self.coarse, small, kernel=9, peak_mask=self.bigmap_mask)
            hint = px*4, py*4
        pad_x = max(patch.shape[1]*1.3/2, search_radius)
        pad_y = max(patch.shape[0]*1.3/2, search_radius)
        x, y = max(0, round(hint[0]-pad_x)), max(0, round(hint[1]-pad_y))
        local = self.map[y:min(self.spec.height, round(hint[1]+pad_y)), x:min(self.spec.width, round(hint[0]+pad_x))]
        px, py, score, detail = self.match(local, patch, mask)
        px, py = px+x+.25, py+y+.25
        # Local peak height measures sharpness, not correctness. Keep both
        # correlation scores for diagnostics; validate motion by continuity.
        # Coordinates here use the half-size map (50 px/s, 0.5 px allowance).
        candidate = (px, py)
        elapsed = time.monotonic()-self.position_time
        rejected = bool(self.previous and elapsed <= 20
                        and np.hypot(px-self.previous.x, py-self.previous.y) >= 50*elapsed+.5)
        if rejected:
            px, py = self.previous.x, self.previous.y
        else:
            self.position_time = time.monotonic()
        # A rejected position is not a failed camera observation. Subtract the
        # map at the retained position and measure the camera independently.
        self.heading_diagnostics = {}
        heading, confidence = self.camera_heading(reference, px, py)
        self.heading_diagnostics.update({"position_method": "speed_rejected" if rejected else "map_correlation",
                                         "candidate_position": list(candidate), "position_elapsed": elapsed,
                                         "score": score, "local_peak": detail})
        self.previous = Pose(px, py, score, detail, heading, confidence)
        return self.previous

    def camera_heading(self, reference, x, y):
        observed = luma(reference[20:224, 79:283])
        scale = self.spec.scale/1.5
        radius = 102*scale
        left, top = max(0, int(x-radius*1.15)), max(0, int(y-radius*1.15))
        background = self.map[top:min(self.spec.height, int(y+radius*1.15)), left:min(self.spec.width, int(x+radius*1.15))]
        background = cv2.resize(background, None, fx=1/scale, fy=1/scale, interpolation=cv2.INTER_LINEAR)
        if min(background.shape) < 204:
            self.heading_diagnostics = {"method": "camera_background_unavailable"}
            return None, 0.0
        rates = cv2.matchTemplate(background, observed, cv2.TM_CCOEFF_NORMED)
        _, _, _, (bx, by) = cv2.minMaxLoc(cv2.resize(rates, None, fx=20, fy=20, interpolation=cv2.INTER_CUBIC))
        yy, xx = np.indices((204, 204), dtype=np.float32)
        aligned = cv2.remap(background, xx+bx/20, yy+by/20, cv2.INTER_LINEAR)
        cone = np.clip((255-aligned.astype(float))/(255-observed.astype(float)+.1)*64, 0, 255).astype(np.uint8)
        cone = cv2.GaussianBlur(cone, (3, 3), 0)
        d = 204
        radial, angular = np.indices((d, d), dtype=np.float32)
        angle = angular*2*np.pi/d
        polar = cv2.remap(cone, d/2+radial/2*np.cos(angle), d/2+radial/2*np.sin(angle), cv2.INTER_LINEAR)
        polar = cv2.resize(polar[d*2//10:d*7//10].astype(np.float32), None, fx=2, fy=2)
        gradient = cv2.Scharr(polar, cv2.CV_32F, 1, 0)
        width = d*2
        rising = np.bincount(find_peaks(gradient.ravel(), height=150, wlen=width)[0] % width, minlength=width)
        falling = np.bincount(find_peaks(-gradient.ravel(), height=150, wlen=width)[0] % width, minlength=width)
        left_edge, right_edge = np.maximum(rising-falling, 0), np.maximum(falling-rising, 0)
        profiles = []
        for shift in range(-3, 4):
            profile = left_edge*circular_smooth(np.roll(right_edge, -width//4+shift), 6)
            profile -= left_edge*circular_smooth(np.roll(right_edge, shift), 20)//5
            profiles.append(circular_smooth(profile, 6))
        profiles = np.maximum(profiles, 1)
        signal = profiles.max(axis=0)
        if prominence(signal) <= .3:
            signal = circular_smooth(signal*profiles.mean(axis=0)*profiles.min(axis=0), 4)
        if not rising.any() or not falling.any() or float(signal.max()) <= 1 or np.ptp(signal) <= 0:
            self.heading_diagnostics = {"method": "cone_boundaries", "edges": 0}
            return None, 0.0
        # Polar coordinates begin east-clockwise; center is 45 degrees after
        # the rising cone boundary. Expose north-clockwise degrees to movement.
        heading = (int(signal.argmax())/width*360+135) % 360
        confidence = prominence(signal)
        self.heading_diagnostics = {"method": "cone_boundaries", "heading": heading, "confidence": confidence}
        return float(heading), confidence

