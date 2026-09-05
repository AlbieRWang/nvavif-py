use avif_serialize::Aviffy;
use ffmpeg_next as ffmpeg;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rayon::prelude::*;
use std::sync::atomic::{AtomicBool, AtomicI8, Ordering};

use ffmpeg::Dictionary;
use ffmpeg::codec::packet::Packet;
use ffmpeg::frame::video::Video;
use ffmpeg::util::format::pixel::Pixel;
use ffmpeg_next::codec::packet::traits::Mut;
use ffmpeg_next::sys as ff_sys;

use std::sync::Once;
static FFMPEG_INIT: Once = Once::new();

// -1 (unchecked), 0 (no), 1 (yes)
static HW_SUPPORT: AtomicI8 = AtomicI8::new(-1);
static WARNED_CPU: AtomicBool = AtomicBool::new(false);

fn ensure_ffmpeg_init() {
    FFMPEG_INIT.call_once(|| {
        ffmpeg::init().expect("libavcodec initialization failed");
        ffmpeg::util::log::set_level(ffmpeg::util::log::Level::Error);
    });
}

#[pyclass(name = "ColorDepth")]
#[derive(Clone, Copy, PartialEq)]
pub enum PyColorDepth {
    #[pyo3(name = "EIGHT_BIT")]
    EightBit,
    #[pyo3(name = "TEN_BIT")]
    TenBit,
}

#[pyclass(name = "Chroma")]
#[derive(Clone, Copy, PartialEq)]
pub enum PyChroma {
    #[pyo3(name = "YUV420")]
    YUV420,
    #[pyo3(name = "YUV444")]
    YUV444,
}

#[pyclass(name = "ColorMatrix")]
#[derive(Clone, Copy, PartialEq)]
pub enum PyColorMatrix {
    #[pyo3(name = "BT601")]
    Bt601,
    #[pyo3(name = "BT709")]
    Bt709,
    #[pyo3(name = "BT2020")]
    Bt2020,
}

struct MatrixCoefs {
    yr: f32,
    yg: f32,
    yb: f32,
    ur: f32,
    ug: f32,
    ub: f32,
    vr: f32,
    vg: f32,
    vb: f32,
}

impl MatrixCoefs {
    fn new(matrix: PyColorMatrix) -> Self {
        match matrix {
            PyColorMatrix::Bt601 => Self {
                yr: 0.299,
                yg: 0.587,
                yb: 0.114,
                ur: -0.168736,
                ug: -0.331264,
                ub: 0.5,
                vr: 0.5,
                vg: -0.418688,
                vb: -0.081312,
            },
            PyColorMatrix::Bt709 => Self {
                yr: 0.2126,
                yg: 0.7152,
                yb: 0.0722,
                ur: -0.114572,
                ug: -0.385428,
                ub: 0.5,
                vr: 0.5,
                vg: -0.454153,
                vb: -0.045847,
            },
            PyColorMatrix::Bt2020 => Self {
                yr: 0.2627,
                yg: 0.6780,
                yb: 0.0593,
                ur: -0.13963,
                ug: -0.36037,
                ub: 0.5,
                vr: 0.5,
                vg: -0.45979,
                vb: -0.04021,
            },
        }
    }
}

struct YuvData {
    y: Vec<u8>,
    u: Vec<u8>,
    v: Vec<u8>,
    pixel_format: Pixel,
}

trait PixelReader: Sync + Send {
    fn read(&self, idx: usize) -> (f32, f32, f32, f32);
}

struct ReaderU8<'a> {
    data: &'a [u8],
    channels: usize,
}
impl PixelReader for ReaderU8<'_> {
    #[inline(always)]
    fn read(&self, idx: usize) -> (f32, f32, f32, f32) {
        let r = self.data[idx] as f32 / 255.0;
        let g = self.data[idx + 1] as f32 / 255.0;
        let b = self.data[idx + 2] as f32 / 255.0;
        let a = if self.channels == 4 {
            self.data[idx + 3] as f32 / 255.0
        } else {
            1.0
        };
        (r, g, b, a)
    }
}

struct ReaderU16<'a> {
    data: &'a [u16],
    channels: usize,
}
impl PixelReader for ReaderU16<'_> {
    #[inline(always)]
    fn read(&self, idx: usize) -> (f32, f32, f32, f32) {
        let r = self.data[idx] as f32 / 65535.0;
        let g = self.data[idx + 1] as f32 / 65535.0;
        let b = self.data[idx + 2] as f32 / 65535.0;
        let a = if self.channels == 4 {
            self.data[idx + 3] as f32 / 65535.0
        } else {
            1.0
        };
        (r, g, b, a)
    }
}

struct ReaderF32<'a> {
    data: &'a [f32],
    channels: usize,
}
impl PixelReader for ReaderF32<'_> {
    #[inline(always)]
    fn read(&self, idx: usize) -> (f32, f32, f32, f32) {
        // Cinematic Tonemapper ACES Filmic (Academy of Motion Picture Arts)
        // Perfectly compresses >1.0 super-bright HDR into SDR bounds without loss of detail
        let map_aces = |x: f32| -> f32 {
            let x = x.max(0.0);
            ((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)).clamp(0.0, 1.0)
        };
        let r = map_aces(self.data[idx]);
        let g = map_aces(self.data[idx + 1]);
        let b = map_aces(self.data[idx + 2]);
        let a = if self.channels == 4 {
            self.data[idx + 3].clamp(0.0, 1.0)
        } else {
            1.0
        };
        (r, g, b, a)
    }
}

/// Determination of hardware acceleration support for AV1 NVENC encoding.
/// Execution of a trial initialization sequence using FFmpeg to validate codec functionality.
/// Caching of the detection result in an atomic variable to prevent redundant probing overhead.
#[pyfunction]
fn is_hardware_supported() -> bool {
    let val = HW_SUPPORT.load(Ordering::Relaxed);
    if val != -1 {
        return val == 1;
    }

    ensure_ffmpeg_init();
    let mut supported = false;
    if ffmpeg_next::encoder::find_by_name("av1_nvenc").is_some() {
        let probe = YuvData {
            y: vec![0; 256 * 256],
            u: vec![128; (256 / 2) * (256 / 2) * 2],
            v: vec![],
            pixel_format: Pixel::NV12,
        };
        supported = encode_av1_frame_gpu(256, 256, &probe, 20, 1).is_ok();
    }
    HW_SUPPORT.store(if supported { 1 } else { 0 }, Ordering::Relaxed);
    supported
}

/// Extraction of YUV420 data from a pixel source.
/// Transformation of pixel data into luminance and interleaved chrominance planes based on specified color matrix coefficients and bit depth.
/// Mapping of alpha channel to the luminance plane if requested.
/// Resulting format is NV12 for 8-bit depth or P010LE for 10-bit depth.
/// Parallelized row-wise processing.
fn extract_yuv420<R: PixelReader>(
    reader: &R,
    width: usize,
    height: usize,
    channels: usize,
    depth: PyColorDepth,
    matrix: PyColorMatrix,
    is_alpha: bool,
) -> YuvData {
    let num_pixels = width * height;
    let coefs = MatrixCoefs::new(matrix);

    match depth {
        PyColorDepth::EightBit => {
            let mut y_plane = vec![0u8; num_pixels];
            let mut uv_plane = vec![128u8; (width / 2) * (height / 2) * 2];

            y_plane
                .par_chunks_mut(width)
                .enumerate()
                .for_each(|(y, row)| {
                    for x in 0..width {
                        let idx = (y * width + x) * channels;
                        let (r, g, b, a) = reader.read(idx);
                        if is_alpha {
                            row[x] = (a * 255.0).clamp(0.0, 255.0) as u8;
                        } else {
                            row[x] = ((coefs.yr * r + coefs.yg * g + coefs.yb * b) * 255.0)
                                .clamp(0.0, 255.0) as u8;
                        }
                    }
                });

            if !is_alpha {
                uv_plane
                    .par_chunks_mut(width)
                    .enumerate()
                    .for_each(|(y_sub, row_uv)| {
                        for x_sub in 0..(width / 2) {
                            let idx = (y_sub * 2 * width + x_sub * 2) * channels;
                            let (r, g, b, _a) = reader.read(idx);
                            row_uv[x_sub * 2] =
                                ((coefs.ur * r + coefs.ug * g + coefs.ub * b) * 255.0 + 128.0)
                                    .clamp(0.0, 255.0) as u8;
                            row_uv[x_sub * 2 + 1] =
                                ((coefs.vr * r + coefs.vg * g + coefs.vb * b) * 255.0 + 128.0)
                                    .clamp(0.0, 255.0) as u8;
                        }
                    });
            }
            YuvData {
                y: y_plane,
                u: uv_plane,
                v: vec![],
                pixel_format: Pixel::NV12,
            }
        }
        PyColorDepth::TenBit => {
            let mut y_plane = vec![0u16; num_pixels];
            let mut uv_plane = vec![512u16 << 6; (width / 2) * (height / 2) * 2];

            y_plane
                .par_chunks_mut(width)
                .enumerate()
                .for_each(|(y, row)| {
                    for x in 0..width {
                        let idx = (y * width + x) * channels;
                        let (r, g, b, a) = reader.read(idx);
                        if is_alpha {
                            row[x] = ((a * 1023.0).clamp(0.0, 1023.0) as u16) << 6;
                        } else {
                            row[x] = (((coefs.yr * r + coefs.yg * g + coefs.yb * b) * 1023.0)
                                .clamp(0.0, 1023.0) as u16)
                                << 6;
                        }
                    }
                });

            if !is_alpha {
                uv_plane
                    .par_chunks_mut(width)
                    .enumerate()
                    .for_each(|(y_sub, row_uv)| {
                        for x_sub in 0..(width / 2) {
                            let idx = (y_sub * 2 * width + x_sub * 2) * channels;
                            let (r, g, b, _a) = reader.read(idx);
                            let u_val =
                                (coefs.ur * r + coefs.ug * g + coefs.ub * b) * 1023.0 + 512.0;
                            let v_val =
                                (coefs.vr * r + coefs.vg * g + coefs.vb * b) * 1023.0 + 512.0;
                            row_uv[x_sub * 2] = (u_val.clamp(0.0, 1023.0) as u16) << 6;
                            row_uv[x_sub * 2 + 1] = (v_val.clamp(0.0, 1023.0) as u16) << 6;
                        }
                    });
            }
            YuvData {
                y: bytemuck::cast_slice(&y_plane).to_vec(),
                u: bytemuck::cast_slice(&uv_plane).to_vec(),
                v: vec![],
                pixel_format: Pixel::P010LE,
            }
        }
    }
}

/// Extraction of YUV444 data from a pixel source.
/// Transformation of pixel data into planar luminance (Y) and chrominance (U, V) components based on specified color matrix coefficients and bit depth.
/// Mapping of the alpha channel to the luminance plane if requested.
/// Resulting format is YUV444P for 8-bit depth or YUV444P16LE for 10-bit depth.
/// Parallelized row-wise processing.
fn extract_yuv444<R: PixelReader>(
    reader: &R,
    width: usize,
    height: usize,
    channels: usize,
    depth: PyColorDepth,
    matrix: PyColorMatrix,
    is_alpha: bool,
) -> YuvData {
    let num_pixels = width * height;
    let coefs = MatrixCoefs::new(matrix);

    match depth {
        PyColorDepth::EightBit => {
            let mut y = vec![0u8; num_pixels];
            let mut u = vec![128u8; num_pixels];
            let mut v = vec![128u8; num_pixels];

            y.par_chunks_mut(width)
                .zip(u.par_chunks_mut(width))
                .zip(v.par_chunks_mut(width))
                .enumerate()
                .for_each(|(y_idx, ((row_y, row_u), row_v))| {
                    for x in 0..width {
                        let idx = (y_idx * width + x) * channels;
                        let (r, g, b, a) = reader.read(idx);
                        if is_alpha {
                            row_y[x] = (a * 255.0).clamp(0.0, 255.0) as u8;
                        } else {
                            row_y[x] = ((coefs.yr * r + coefs.yg * g + coefs.yb * b) * 255.0)
                                .clamp(0.0, 255.0) as u8;
                            row_u[x] = ((coefs.ur * r + coefs.ug * g + coefs.ub * b) * 255.0
                                + 128.0)
                                .clamp(0.0, 255.0) as u8;
                            row_v[x] = ((coefs.vr * r + coefs.vg * g + coefs.vb * b) * 255.0
                                + 128.0)
                                .clamp(0.0, 255.0) as u8;
                        }
                    }
                });
            YuvData {
                y,
                u,
                v,
                pixel_format: Pixel::YUV444P,
            }
        }
        PyColorDepth::TenBit => {
            let mut y = vec![0u16; num_pixels];
            let mut u = vec![512u16 << 6; num_pixels];
            let mut v = vec![512u16 << 6; num_pixels];

            y.par_chunks_mut(width)
                .zip(u.par_chunks_mut(width))
                .zip(v.par_chunks_mut(width))
                .enumerate()
                .for_each(|(y_idx, ((row_y, row_u), row_v))| {
                    for x in 0..width {
                        let idx = (y_idx * width + x) * channels;
                        let (r, g, b, a) = reader.read(idx);
                        if is_alpha {
                            row_y[x] = ((a * 1023.0).clamp(0.0, 1023.0) as u16) << 6;
                        } else {
                            row_y[x] = (((coefs.yr * r + coefs.yg * g + coefs.yb * b) * 1023.0)
                                .clamp(0.0, 1023.0) as u16)
                                << 6;
                            row_u[x] = (((coefs.ur * r + coefs.ug * g + coefs.ub * b) * 1023.0
                                + 512.0)
                                .clamp(0.0, 1023.0) as u16)
                                << 6;
                            row_v[x] = (((coefs.vr * r + coefs.vg * g + coefs.vb * b) * 1023.0
                                + 512.0)
                                .clamp(0.0, 1023.0) as u16)
                                << 6;
                        }
                    }
                });
            YuvData {
                y: bytemuck::cast_slice(&y).to_vec(),
                u: bytemuck::cast_slice(&u).to_vec(),
                v: bytemuck::cast_slice(&v).to_vec(),
                pixel_format: Pixel::YUV444P16LE,
            }
        }
    }
}

/// Alpha planes are simple content (mostly flat masks and soft edges): the
/// quantizer drives fidelity, so the alpha item always encodes with a fast
/// rav1e preset instead of inheriting the slow color preset.
/// Preset 2 maps to rav1e speed 9 (see the preset mapping below).
/// Measured on the 10-image transparent test set (2026-09-05): speed 5
/// baseline 93.7 s -> 6.0 s (15.7x), ~10-25% larger files, alpha MAE still
/// ~0.1/255 on normal images. Speed 8 was 2x slower with no fidelity gain.
const ALPHA_RAV1E_PRESET: i32 = 2;

/// NVENC AV1 input size cap (measured: any width or height above this fails,
/// see DEVELOPMENT_NOTES §12). Images beyond it take the CPU path regardless.
const NVENC_MAX_DIMENSION: usize = 8192;

/// Preset for the CPU color encode of images that exceed the NVENC size cap.
/// The normal CPU color path inherits the (slow) requested preset; for
/// oversized images that makes the fallback path dominant — measured
/// 101.7 MP in 39-50 s (2026-09-05 full-corpus run, 87% of total batch time).
/// Preset 4 maps to rav1e speed 7; quality impact is validated in
/// uvtest/test_oversize_preset.py.
const OVERSIZE_RAV1E_PRESET: i32 = 4;

/// Preset for a CPU color encode: oversized images (which can never take the
/// GPU path) use the dedicated fast preset; everything else keeps the
/// requested preset so a transient NVENC failure does not silently drop
/// quality on normally-sized images.
fn cpu_color_preset(preset: i32, width: usize, height: usize) -> i32 {
    if width > NVENC_MAX_DIMENSION || height > NVENC_MAX_DIMENSION {
        OVERSIZE_RAV1E_PRESET
    } else {
        preset
    }
}

/// Software-based AV1 frame encoding using rav1e.
/// Encoding of YUV pixel data into an AV1 bitstream via CPU.
/// Mapping of NVENC-style quality (CQ) and speed presets to rav1e quantizer and speed parameters.
/// Support for 8-bit and 10-bit color depths with YUV420 and YUV444 chroma subsampling.
/// De-interleaving of chrominance planes for NV12 and P010 formats during processing.
/// Bit-depth normalization for 10-bit P010 input through right-shifting.
/// Multi-threaded execution with forced intra-frame configuration.
fn encode_av1_frame_cpu(
    width: usize,
    height: usize,
    data: &YuvData,
    cq: i32,
    preset: i32,
    depth: PyColorDepth,
    chroma: PyChroma,
    monochrome: bool,
) -> PyResult<Vec<u8>> {
    let mut enc = rav1e::EncoderConfig::default();
    enc.width = width;
    enc.height = height;
    // Heuristic mapping CQ NVENC -> rav1e Quantizer:
    // NVENC CQ 20 (visually lossless) = corresponds to rav1e Quantizer ~50.
    enc.quantizer = (cq as usize * 255) / 100;
    // Do not lock min_quantizer so that rav1e can improve quality on the fly

    // Map NvencPreset (1-7) to rav1e speed (10-0). Preset 7 (Max) -> Speed 4.
    let speed = match preset {
        7 => 4,
        6 => 5,
        5 => 6,
        4 => 7,
        3 => 8,
        2 => 9,
        _ => 10,
    };
    enc.speed_settings = rav1e::config::SpeedSettings::from_preset(speed);
    enc.max_key_frame_interval = 1;
    enc.min_key_frame_interval = 1;
    enc.bit_depth = if depth == PyColorDepth::TenBit { 10 } else { 8 };
    enc.chroma_sampling = if monochrome {
        rav1e::color::ChromaSampling::Cs400
    } else {
        match chroma {
            PyChroma::YUV420 => rav1e::color::ChromaSampling::Cs420,
            PyChroma::YUV444 => rav1e::color::ChromaSampling::Cs444,
        }
    };

    let mut ctx: rav1e::Context<u16> = rav1e::Config::new()
        .with_encoder_config(enc)
        .with_threads(0) // Utilization of all CPU cores
        .new_context()
        .map_err(|e: rav1e::InvalidConfig| {
            pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
        })?;

    let mut frame = ctx.new_frame();

    if depth == PyColorDepth::EightBit {
        frame.planes[0].copy_from_raw_u8(&data.y, width, 1);
        if monochrome {
            // AVIF alpha is a monochrome (YUV400) AV1 item.
        } else if chroma == PyChroma::YUV444 {
            frame.planes[1].copy_from_raw_u8(&data.u, width, 1);
            frame.planes[2].copy_from_raw_u8(&data.v, width, 1);
        } else {
            // On-the-fly deinterleaving NV12 -> I420
            let half_w = width / 2;
            let half_h = height / 2;
            let mut u = vec![0u8; half_w * half_h];
            let mut v = vec![0u8; half_w * half_h];
            for i in 0..(half_w * half_h) {
                u[i] = data.u[i * 2];
                v[i] = data.u[i * 2 + 1];
            }
            frame.planes[1].copy_from_raw_u8(&u, half_w, 1);
            frame.planes[2].copy_from_raw_u8(&v, half_w, 1);
        }
    } else {
        // Remove NVENC P010 offsets (shift >> 6) for 10-bit
        let y16: &[u16] = bytemuck::cast_slice(&data.y);
        let mut y_unpad = vec![0u16; y16.len()];
        for i in 0..y16.len() {
            y_unpad[i] = y16[i] >> 6;
        }
        frame.planes[0].copy_from_raw_u8(bytemuck::cast_slice(&y_unpad), width * 2, 2);

        if monochrome {
            // AVIF alpha is a monochrome (YUV400) AV1 item.
        } else if chroma == PyChroma::YUV444 {
            let u16_in: &[u16] = bytemuck::cast_slice(&data.u);
            let v16_in: &[u16] = bytemuck::cast_slice(&data.v);
            let mut u_unpad = vec![0u16; u16_in.len()];
            let mut v_unpad = vec![0u16; v16_in.len()];
            for i in 0..u16_in.len() {
                u_unpad[i] = u16_in[i] >> 6;
                v_unpad[i] = v16_in[i] >> 6;
            }
            frame.planes[1].copy_from_raw_u8(bytemuck::cast_slice(&u_unpad), width * 2, 2);
            frame.planes[2].copy_from_raw_u8(bytemuck::cast_slice(&v_unpad), width * 2, 2);
        } else {
            let half_w = width / 2;
            let half_h = height / 2;
            let uv16_in: &[u16] = bytemuck::cast_slice(&data.u);
            let mut u_unpad = vec![0u16; half_w * half_h];
            let mut v_unpad = vec![0u16; half_w * half_h];
            for i in 0..(half_w * half_h) {
                u_unpad[i] = uv16_in[i * 2] >> 6;
                v_unpad[i] = uv16_in[i * 2 + 1] >> 6;
            }
            frame.planes[1].copy_from_raw_u8(bytemuck::cast_slice(&u_unpad), half_w * 2, 2);
            frame.planes[2].copy_from_raw_u8(bytemuck::cast_slice(&v_unpad), half_w * 2, 2);
        }
    }

    ctx.send_frame(frame)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rav1e err: {}", e)))?;
    ctx.flush();
    let mut out = Vec::new();
    while let Ok(packet) = ctx.receive_packet() {
        out.extend_from_slice(&packet.data);
    }
    Ok(out)
}

/// AV1 encoding of a single YUV frame via NVIDIA hardware acceleration (NVENC).
/// Configuration of the encoder context for intra-only processing, zero delay, and constant quantization to facilitate still image generation.
/// Mapping of input YuvData planes to hardware-compatible buffers with support for 8-bit and 10-bit depths in 4:2:0 and 4:4:4 formats.
/// Execution of the encoding pipeline and extraction of the resulting bitstream through immediate encoder flushing.
fn encode_av1_frame_gpu(
    width: usize,
    height: usize,
    data: &YuvData,
    cq: i32,
    preset: i32,
) -> PyResult<Vec<u8>> {
    let codec = ffmpeg::encoder::find_by_name("av1_nvenc")
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("NVIDIA AV1 Encoder not found"))?;

    let ctx = ffmpeg::codec::context::Context::new_with_codec(codec);
    let mut video_ctx = ctx.encoder().video().map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Video context error: {}", e))
    })?;

    video_ctx.set_width(width as u32);
    video_ctx.set_height(height as u32);
    video_ctx.set_format(data.pixel_format);
    video_ctx.set_time_base((1, 30));
    // for still image:
    video_ctx.set_gop(0); // intra-only, every frame is key
    video_ctx.set_max_b_frames(0);

    let mut options = Dictionary::new();
    options.set("preset", &format!("p{}", preset));
    options.set("rc", "constqp");
    options.set("qp", &cq.to_string());
    options.set("tune", "hq");
    options.set("bf", "0");
    options.set("delay", "0");
    options.set("forced-idr", "1");
    options.set("rc-lookahead", "0");
    if data.pixel_format == Pixel::YUV444P || data.pixel_format == Pixel::YUV444P16LE {
        // av1_nvenc exposes AV1 profiles as integer values. Profile 1 is
        // High, which is required for 4:4:4 input in the AV1 bitstream.
        options.set("profile", "1");
    }

    let mut encoder = video_ctx.open_as_with(codec, options).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("NVENC init failed: {}", e))
    })?;

    let mut frame = Video::empty();
    unsafe {
        frame.alloc(data.pixel_format, width as u32, height as u32);
    }

    let planes = if data.v.is_empty() { 2 } else { 3 };
    let sources = [&data.y, &data.u, &data.v];

    for i in 0..planes {
        let stride = frame.stride(i);
        let dst = frame.data_mut(i);
        let bytes_per_px =
            if data.pixel_format == Pixel::P010LE || data.pixel_format == Pixel::YUV444P16LE {
                2
            } else {
                1
            };
        let row_size = width * bytes_per_px;

        // Preventing OOB panic for UV planes in 4:2:0 format
        let plane_height = match (data.pixel_format, i) {
            (Pixel::NV12, 1) | (Pixel::P010LE, 1) => height / 2,
            _ => height,
        };

        for y in 0..plane_height {
            dst[y * stride..y * stride + row_size]
                .copy_from_slice(&sources[i][y * row_size..y * row_size + row_size]);
        }
    }

    encoder.send_frame(&frame).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to send frame: {}", e))
    })?;
    encoder.send_eof().map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to send EOF: {}", e))
    })?;

    let mut packet = Packet::empty();
    let mut out = Vec::new();
    while encoder.receive_packet(&mut packet).is_ok() {
        if let Some(d) = packet.data() {
            out.extend_from_slice(d);
        }
    }

    if out.is_empty() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "NVENC returned empty packet",
        ));
    }

    Ok(out)
}

/// Calculation of statistical variance for a square region within a luminance plane.
/// Accumulation of pixel intensity sums and squared sums over the specified spatial coordinates.
/// Derivation of variance using the difference between the mean of squares and the square of the mean.
fn block_variance(y_plane: &[u8], stride: usize, x: usize, y: usize, block_size: usize) -> f32 {
    let mut sum: u64 = 0;
    let mut sum_sq: u64 = 0;

    for by in 0..block_size {
        let row_offset = (y + by) * stride;
        for bx in 0..block_size {
            let val = y_plane[row_offset + x + bx] as u64;
            sum += val;
            sum_sq += val * val;
        }
    }

    let n = (block_size * block_size) as f64;
    let mean = sum as f64 / n;
    let var = (sum_sq as f64 / n) - (mean * mean);
    var as f32
}

/// Super-fast calculation of Structural Similarity Index (SSIM) for 8-bit luminance channels.
/// Evaluation of image quality using non-overlapping 8x8 spatial windows and standard stability constants.
/// Parallel execution via row-wise partitioning of block calculations.
/// Derivation of local mean, variance, and covariance for each block to compute the similarity index.
/// Normalization of the aggregate SSIM score by the total number of processed blocks.
fn calculate_ssim(
    y_orig: &[u8],
    orig_stride: usize,
    y_dec: &[u8],
    dec_stride: usize,
    width: usize,
    height: usize,
) -> f64 {
    const BLOCK_SIZE: usize = 8;
    const C1: f64 = 6.5025;
    const C2: f64 = 58.5225;

    let blocks_x = width / BLOCK_SIZE;
    let blocks_y = height / BLOCK_SIZE;

    let total_ssim: f64 = (0..blocks_y)
        .into_par_iter()
        .map(|by| {
            let mut row_ssim = 0.0;
            let y_offset_orig = by * BLOCK_SIZE;
            let y_offset_dec = by * BLOCK_SIZE;

            for bx in 0..blocks_x {
                let x_offset = bx * BLOCK_SIZE;

                let mut sum_x: u32 = 0;
                let mut sum_y: u32 = 0;
                let mut sum_xx: u32 = 0;
                let mut sum_yy: u32 = 0;
                let mut sum_xy: u32 = 0;

                for r in 0..BLOCK_SIZE {
                    let idx_orig = (y_offset_orig + r) * orig_stride + x_offset;
                    let idx_dec = (y_offset_dec + r) * dec_stride + x_offset;
                    for c in 0..BLOCK_SIZE {
                        let px = y_orig[idx_orig + c] as u32;
                        let py = y_dec[idx_dec + c] as u32;

                        sum_x += px;
                        sum_y += py;
                        sum_xx += px * px;
                        sum_yy += py * py;
                        sum_xy += px * py;
                    }
                }

                let num_pixels = (BLOCK_SIZE * BLOCK_SIZE) as f64;
                let mean_x = sum_x as f64 / num_pixels;
                let mean_y = sum_y as f64 / num_pixels;

                let var_x = (sum_xx as f64 / num_pixels) - (mean_x * mean_x);
                let var_y = (sum_yy as f64 / num_pixels) - (mean_y * mean_y);
                let cov_xy = (sum_xy as f64 / num_pixels) - (mean_x * mean_y);

                let num = (2.0 * mean_x * mean_y + C1) * (2.0 * cov_xy + C2);
                let den = (mean_x * mean_x + mean_y * mean_y + C1) * (var_x + var_y + C2);

                row_ssim += num / den;
            }
            row_ssim
        })
        .sum();

    total_ssim / ((blocks_x * blocks_y) as f64).max(1.0)
}

/// Assembly of a 512x512 mosaic frame from the four most informative 256x256 patches of the input luminance plane.
/// Normalization of input data to 8-bit depth, including downscaling from 10-bit representations.
/// Selection of patches based on a combination of central priority and maximum local variance to capture high-detail regions.
/// Enforcement of spatial diversity through overlap constraints during patch selection.
/// Tiling of extracted regions into a fixed-size quad-layout buffer.
fn prepare_trial_frame_8bit(
    yuv_y: &[u8],
    width: usize,
    height: usize,
    depth: PyColorDepth,
) -> Vec<u8> {
    let mut y8 = vec![0u8; width * height];
    if depth == PyColorDepth::TenBit {
        let y16: &[u16] = bytemuck::cast_slice(yuv_y);
        for i in 0..y16.len() {
            y8[i] = (y16[i] >> 8) as u8;
        }
    } else {
        y8.copy_from_slice(yuv_y);
    }

    let trial_dim = 512;
    let mut trial_y = vec![0u8; trial_dim * trial_dim];
    let patch_size = 256;
    let num_patches = 4;
    let mut selected = Vec::new();

    if width >= patch_size && height >= patch_size {
        let mut variances = Vec::new();
        for y in (0..height.saturating_sub(patch_size)).step_by(patch_size / 2) {
            for x in (0..width.saturating_sub(patch_size)).step_by(patch_size / 2) {
                let var = block_variance(&y8, width, x, y, patch_size);
                variances.push((var, x, y));
            }
        }
        variances.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        // central part
        selected.push((width / 2 - patch_size / 2, height / 2 - patch_size / 2));

        for &(_, px, py) in &variances {
            if selected.len() >= num_patches {
                break;
            }
            let overlap = selected.iter().any(|&(sx, sy)| {
                (px as isize - sx as isize).abs() < (patch_size / 2) as isize
                    && (py as isize - sy as isize).abs() < (patch_size / 2) as isize
            });
            if !overlap {
                selected.push((px, py));
            }
        }
    }
    // Failsafe for small images
    while selected.len() < num_patches {
        selected.push((0, 0));
    }

    let offsets = [(0, 0), (256, 0), (0, 256), (256, 256)];
    for (i, &(px, py)) in selected.iter().enumerate() {
        let (dx, dy) = offsets[i];
        for by in 0..patch_size {
            let sy = py + by;
            if sy < height {
                let copy_w = width.saturating_sub(px).min(patch_size);
                let dst_idx = (dy + by) * trial_dim + dx;
                let src_idx = sy * width + px;
                trial_y[dst_idx..dst_idx + copy_w].copy_from_slice(&y8[src_idx..src_idx + copy_w]);
            }
        }
    }
    trial_y
}

/// Evaluation of AV1 encoding quality for a specific Constant Quantizer level.
/// Execution of a complete pipeline consisting of frame encoding, bitstream decoding, and SSIM calculation.
/// Selection between hardware-accelerated (NVENC) and software encoding paths based on the specified hardware flag.
/// Re-decoding of the generated bitstream via libdav1d to facilitate comparison between the reconstructed luma plane and the original source data.
/// Resulting SSIM value calculation for quality assessment at the given quantization parameter.
fn check_cq_ssim(
    cq: i32,
    trial_y: &[u8],
    trial_dim: usize,
    preset: i32,
    use_gpu: bool,
) -> PyResult<f64> {
    let av1_data = if use_gpu {
        let data = YuvData {
            y: trial_y.to_vec(),
            u: vec![128; (trial_dim / 2) * (trial_dim / 2) * 2],
            v: vec![],
            pixel_format: Pixel::NV12,
        };
        encode_av1_frame_gpu(trial_dim, trial_dim, &data, cq, preset)?
    } else {
        // CPU cost in fractions of milliseconds (max speed = 10)
        let data = YuvData {
            y: trial_y.to_vec(),
            u: vec![128; (trial_dim / 2) * (trial_dim / 2) * 2],
            v: vec![],
            pixel_format: Pixel::NV12,
        };
        encode_av1_frame_cpu(
            trial_dim,
            trial_dim,
            &data,
            cq,
            1,
            PyColorDepth::EightBit,
            PyChroma::YUV420,
            false,
        )?
    };

    // Decoding (dav1d)
    let decoder_codec = ffmpeg::decoder::find_by_name("libdav1d").unwrap();
    let mut decoder_ctx = ffmpeg::codec::context::Context::new_with_codec(decoder_codec)
        .decoder()
        .video()
        .unwrap();

    let mut computed_ssim = 0.0;
    let mut pkt = ffmpeg::codec::packet::Packet::empty();

    // Safe byte injection into an FFmpeg packet for testing in dav1d
    unsafe {
        ff_sys::av_new_packet(pkt.as_mut_ptr(), av1_data.len() as i32);
        std::ptr::copy_nonoverlapping(av1_data.as_ptr(), (*pkt.as_mut_ptr()).data, av1_data.len());
    }

    if decoder_ctx.send_packet(&pkt).is_ok() {
        let mut decoded = ffmpeg::frame::Video::empty();
        while decoder_ctx.receive_frame(&mut decoded).is_ok() {
            let dec_y = decoded.data(0);
            let dec_stride = decoded.stride(0);
            computed_ssim =
                calculate_ssim(trial_y, trial_dim, dec_y, dec_stride, trial_dim, trial_dim);
        }
    }

    Ok(computed_ssim)
}

/// Estimation of the optimal Constant Quality (CQ) parameter via a two-point secant approximation.
/// Evaluation of the SSIM-to-CQ curve slope using a fixed mid-range anchor and a dynamically selected second probe based on the target quality.
/// Integration of a safeguard mechanism to prevent bitrate explosion on noisy or artifact-heavy sources by capping the CQ value when SSIM improvements are marginal.
/// Clamping of the final result within the 0–51 range.
fn estimate_cq(
    y_ref: &[u8],
    width: usize,
    height: usize,
    depth: PyColorDepth,
    target_ssim: f64,
    preset: i32,
    use_gpu: bool,
) -> PyResult<i32> {
    let trial_y = prepare_trial_frame_8bit(y_ref, width, height, depth);
    let trial_dim = 512;

    // Anchor (Middle of the range)
    let cq1 = 28;
    let ssim1 = check_cq_ssim(cq1, &trial_y, trial_dim, preset, use_gpu)?;

    // Choose dynamically depending on where the target pulls
    let cq2 = if target_ssim > ssim1 {
        16 // Target above the anchor: need to test high quality
    } else {
        44 // Target below the anchor: test aggressive compression
    };
    let ssim2 = check_cq_ssim(cq2, &trial_y, trial_dim, preset, use_gpu)?;

    eprintln!(
        "[nvavif_py] Estimate Pass 1 (CQ {}): SSIM {:.4}",
        cq1, ssim1
    );
    eprintln!(
        "[nvavif_py] Estimate Pass 2 (CQ {}): SSIM {:.4}",
        cq2, ssim2
    );

    let diff = ssim2 - ssim1;
    if diff.abs() < 0.0001 {
        eprintln!("[nvavif_py] Curve is dead flat. Safe-fallback to Anchor (CQ 28)");
        return Ok(28);
    }

    let slope = (cq2 as f64 - cq1 as f64) / diff;
    let mut target_cq = cq1 as f64 + (target_ssim - ssim1) * slope;

    // Bitrate Safeguard
    // If increased from CQ=28 to CQ=16, but SSIM grew microscopically (by < 0.015)
    // It means we are trying to encode noise/JPEG artifacts. The curve became an asymptote.
    if target_ssim > ssim1 && cq2 < cq1 {
        let ssim_gain = ssim2 - ssim1;
        if ssim_gain < 0.015 {
            eprintln!(
                "[nvavif_py] SAFEGUARD TRIGGERED: Source contains heavy noise/artifacts. Capping max bitrate."
            );
            target_cq = target_cq.max(16.0); // Do not let the codec drop to CQ=0 and create a 15 MB file
        }
    }

    let final_cq = target_cq.round().clamp(0.0, 51.0) as i32;
    eprintln!(
        "[nvavif_py] Math Target: {:.2} -> Choosed Clamped CQ: {}",
        target_cq, final_cq
    );

    Ok(final_cq)
}

/// Construction of YUV color and optional alpha planes from a pixel source.
/// Selection of extraction routine based on specified chroma subsampling.
/// Conditional extraction of the alpha channel into a separate YUV structure if four channels are present.
#[inline(always)]
fn build_yuv<R: PixelReader>(
    reader: &R,
    width: usize,
    height: usize,
    channels: usize,
    depth: PyColorDepth,
    matrix: PyColorMatrix,
    chroma: PyChroma,
) -> (YuvData, Option<YuvData>) {
    let color = match chroma {
        PyChroma::YUV420 => extract_yuv420(reader, width, height, channels, depth, matrix, false),
        PyChroma::YUV444 => extract_yuv444(reader, width, height, channels, depth, matrix, false),
    };
    let alpha = if channels == 4 {
        Some(match chroma {
            PyChroma::YUV420 => {
                extract_yuv420(reader, width, height, channels, depth, matrix, true)
            }
            PyChroma::YUV444 => {
                extract_yuv444(reader, width, height, channels, depth, matrix, true)
            }
        })
    } else {
        None
    };
    (color, alpha)
}

/// Encoding of raw pixel data into an AVIF bitstream.
/// Automatic selection between hardware-accelerated (NVENC) and software (rav1e) encoders based on hardware availability and device parameters.
/// Zero-copy transformation of input buffers (u8, u16, f32) into YUV planes using memory casting.
/// Provision for SSIM-based quality calibration, independent alpha channel compression, and embedding of CICP color metadata and EXIF segments.
#[pyfunction]
#[pyo3(signature = (pixels, width, height, input_dtype, cq=20, auto_cq=false, target_ssim=0.985, alpha_cq=None, preset=6, depth=PyColorDepth::TenBit, chroma=PyChroma::YUV444, matrix=PyColorMatrix::Bt709, exif=None, device="auto"))]
#[allow(clippy::too_many_arguments)]
fn encode_avif(
    py: Python<'_>,
    pixels: &[u8],
    width: usize,
    height: usize,
    input_dtype: &str,
    cq: i32,
    auto_cq: bool,
    target_ssim: f64,
    alpha_cq: Option<i32>,
    preset: i32,
    depth: PyColorDepth,
    chroma: PyChroma,
    matrix: PyColorMatrix,
    exif: Option<&[u8]>,
    device: &str, // "auto", "gpu", "cpu"
) -> PyResult<Py<PyBytes>> {
    ensure_ffmpeg_init();

    // Fallback router
    let has_gpu = is_hardware_supported();
    let mut use_gpu = match device {
        "gpu" => {
            if !has_gpu {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "GPU encoding requested but NVENC AV1 not supported",
                ));
            }
            true
        }
        "cpu" => false,
        _ => has_gpu, // auto fallback
    };

    // Calculate the number of channels based on the type and size of the raw array:
    let px_count = width * height;
    let channels = match input_dtype {
        "u8" => pixels.len() / px_count,
        "u16" => (pixels.len() / 2) / px_count,
        "f32" => (pixels.len() / 4) / px_count,
        _ => {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Unknown input_dtype",
            ));
        }
    };
    let allow_gpu_fallback = device != "gpu";

    // Instantiate the required reader via `bytemuck` without copying RAM
    // with maximum static optimization (Zero-Cost Abstractions)
    let (color_yuv, alpha_yuv) = match input_dtype {
        "u8" => build_yuv(
            &ReaderU8 {
                data: pixels,
                channels,
            },
            width,
            height,
            channels,
            depth,
            matrix,
            chroma,
        ),
        "u16" => build_yuv(
            &ReaderU16 {
                data: bytemuck::cast_slice(pixels),
                channels,
            },
            width,
            height,
            channels,
            depth,
            matrix,
            chroma,
        ),
        "f32" => build_yuv(
            &ReaderF32 {
                data: bytemuck::cast_slice(pixels),
                channels,
            },
            width,
            height,
            channels,
            depth,
            matrix,
            chroma,
        ),
        _ => unreachable!(),
    };

    let bit_depth = if depth == PyColorDepth::TenBit { 10 } else { 8 };

    // Modify CICP metadata to add BT2020 labeling
    let avif_bytes = py.detach(move || -> PyResult<Vec<u8>> {
        // Logs of the fallback router
        if !use_gpu && !WARNED_CPU.swap(true, Ordering::Relaxed) {
            eprintln!("[nvavif_py] INFO: NVENC is missing or disabled. Seamlessly falling back to CPU rav1e software encoder.");
        }

        let final_cq = if auto_cq {
            if !use_gpu {
                // If CPU encoding is selected, warn that calibration will take time (once)
                eprintln!("[nvavif_py] INFO: Estimating auto-CQ via CPU. This may take ~200-500ms extra.");
            }
            match estimate_cq(&color_yuv.y, width, height, depth, target_ssim, preset, use_gpu) {
                Ok(cq) => cq,
                Err(error) if use_gpu && allow_gpu_fallback => {
                    eprintln!(
                        "[nvavif_py] WARN: NVENC auto-CQ probe failed ({}); falling back to CPU.",
                        error
                    );
                    use_gpu = false;
                    eprintln!("[nvavif_py] INFO: Estimating auto-CQ via CPU. This may take ~200-500ms extra.");
                    estimate_cq(
                        &color_yuv.y,
                        width,
                        height,
                        depth,
                        target_ssim,
                        cpu_color_preset(preset, width, height),
                        false,
                    )
                    .unwrap_or(cq)
                }
                Err(error) => return Err(error),
            }
        } else {
            cq
        };

        let (color_av1, alpha_av1) = match alpha_yuv {
            Some(alpha_data) if use_gpu => {
                // NVENC does not expose a monochrome/YUV400 AV1 input, while
                // AVIF alpha items must be monochrome. Encode the two items
                // concurrently so the CPU alpha path does not serialize the
                // otherwise GPU-bound color encode.
                let a_cq = alpha_cq.unwrap_or_else(|| (final_cq - 4).clamp(0, 51));
                let (color_result, alpha_result) = std::thread::scope(|scope| {
                    let color_job = scope.spawn(|| {
                        encode_av1_frame_gpu(width, height, &color_yuv, final_cq, preset)
                            .map_err(|e| e.to_string())
                    });
                    let alpha_job = scope.spawn(move || {
                        encode_av1_frame_cpu(
                            width,
                            height,
                            &alpha_data,
                            a_cq,
                            ALPHA_RAV1E_PRESET,
                            depth,
                            chroma,
                            true,
                        )
                        .map_err(|e| e.to_string())
                    });
                    (color_job.join(), alpha_job.join())
                });

                let color_result = color_result
                    .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("GPU color encode thread panicked"))?;
                let color_av1 = match color_result {
                    Ok(data) => data,
                    Err(error) if allow_gpu_fallback => {
                        eprintln!(
                            "[nvavif_py] WARN: NVENC color encode failed ({}); falling back to CPU.",
                            error
                        );
                        encode_av1_frame_cpu(
                            width,
                            height,
                            &color_yuv,
                            final_cq,
                            cpu_color_preset(preset, width, height),
                            depth,
                            chroma,
                            false,
                        )?
                    }
                    Err(error) => return Err(pyo3::exceptions::PyRuntimeError::new_err(error)),
                };
                let alpha_av1 = alpha_result
                    .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("CPU alpha encode thread panicked"))?
                    .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
                (color_av1, Some(alpha_av1))
            }
            Some(alpha_data) => {
                let color_av1 = encode_av1_frame_cpu(
                    width,
                    height,
                    &color_yuv,
                    final_cq,
                    preset,
                    depth,
                    chroma,
                    false,
                )?;
                let a_cq = alpha_cq.unwrap_or_else(|| (final_cq - 4).clamp(0, 51));
                let alpha_av1 = encode_av1_frame_cpu(
                    width,
                    height,
                    &alpha_data,
                    a_cq,
                    ALPHA_RAV1E_PRESET,
                    depth,
                    chroma,
                    true,
                )?;
                (color_av1, Some(alpha_av1))
            }
            None => {
                let color_av1 = if use_gpu {
                    match encode_av1_frame_gpu(width, height, &color_yuv, final_cq, preset) {
                        Ok(data) => data,
                        Err(error) if allow_gpu_fallback => {
                            eprintln!(
                                "[nvavif_py] WARN: NVENC color encode failed ({}); falling back to CPU.",
                                error
                            );
                            encode_av1_frame_cpu(
                                width,
                                height,
                                &color_yuv,
                                final_cq,
                                cpu_color_preset(preset, width, height),
                                depth,
                                chroma,
                                false,
                            )?
                        }
                        Err(error) => return Err(error),
                    }
                } else {
                    encode_av1_frame_cpu(
                        width,
                        height,
                        &color_yuv,
                        final_cq,
                        preset,
                        depth,
                        chroma,
                        false,
                    )?
                };
                (color_av1, None)
            }
        };

        // Embed color space metadata (CICP)
        let cicp_matrix = match matrix {
            PyColorMatrix::Bt601 => avif_serialize::constants::MatrixCoefficients::Bt601,
            PyColorMatrix::Bt709 => avif_serialize::constants::MatrixCoefficients::Bt709,
            PyColorMatrix::Bt2020 => avif_serialize::constants::MatrixCoefficients::Bt2020Ncl,
        };
        // Primaries (color triangle boundaries) are taken from the matrix
        let cicp_primaries = match matrix {
            PyColorMatrix::Bt2020 => avif_serialize::constants::ColorPrimaries::Bt2020,
            _ => avif_serialize::constants::ColorPrimaries::Bt709,
        };

        // Create the serializer as a mutable variable
        let mut aviffy = Aviffy::new();
        aviffy.matrix_coefficients(cicp_matrix)
            .transfer_characteristics(avif_serialize::constants::TransferCharacteristics::Srgb)
            .color_primaries(cicp_primaries)
            .set_chroma_subsampling(match chroma {
                PyChroma::YUV420 => (true, true),
                PyChroma::YUV444 => (false, false),
            })
            .premultiplied_alpha(false);

        // Integrate only what is supported by the avif-serialize container: EXIF
        if let Some(e) = exif {
            aviffy.set_exif(e.to_vec());
        }

        let avif = aviffy.to_vec(&color_av1, alpha_av1.as_deref(), width as u32, height as u32, bit_depth);

        Ok(avif)
    })?;

    Ok(PyBytes::new(py, &avif_bytes).into())
}

/// Parallel conversion of YUV video frames to RGB or RGBA pixel data.
/// Transformation of multi-planar YUV data into interleaved RGB(A) buffers using BT.709 fixed-point math.
/// Support for 8-bit and 10-bit input depths across 4:2:0, 4:2:2, and 4:4:4 subsampling schemes.
/// Normalization of 10-bit data to 8-bit output range.
/// Row-level parallelization via segmented chunk processing.
/// Direct implementation to bypass external scaling library overhead.
fn yuv_to_rgb_parallel(decoded: &ffmpeg::frame::Video) -> PyResult<(Vec<u8>, usize, usize, usize)> {
    let w = decoded.width() as usize;
    let h = decoded.height() as usize;
    let format = decoded.format();

    let (has_alpha, is_10bit) = match format {
        Pixel::YUV420P | Pixel::YUV422P | Pixel::YUV444P => (false, false),
        Pixel::YUV420P10LE | Pixel::YUV422P10LE | Pixel::YUV444P10LE => (false, true),
        Pixel::YUVA420P | Pixel::YUVA422P | Pixel::YUVA444P => (true, false),
        Pixel::YUVA444P10LE => (true, true),
        other => {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Unsupported: {:?}",
                other
            )));
        }
    };

    let is_444 = matches!(
        format,
        Pixel::YUV444P | Pixel::YUV444P10LE | Pixel::YUVA444P | Pixel::YUVA444P10LE
    );
    let is_422 = matches!(
        format,
        Pixel::YUV422P | Pixel::YUV422P10LE | Pixel::YUVA422P
    );

    let channels = if has_alpha { 4 } else { 3 };

    let y_stride = decoded.stride(0);
    let u_stride = decoded.stride(1);
    let v_stride = decoded.stride(2);
    let y_data = decoded.data(0);
    let u_data = decoded.data(1);
    let v_data = decoded.data(2);
    let a_data = if has_alpha {
        Some(decoded.data(3))
    } else {
        None
    };
    let a_stride = if has_alpha { decoded.stride(3) } else { 0 };

    let mut output = vec![0u8; w * h * channels];

    // Dynamic detection of color space for perfect color reproduction
    let color_space = decoded.color_space();

    // BT.709 constants multiplied by 1024 (Fixed point math) by default
    let (c_cr_r, c_cb_g, c_cr_g, c_cb_b) = match color_space {
        // BT.2020 (HDR / Wide Gamut)
        ffmpeg::color::Space::BT2020NCL | ffmpeg::color::Space::BT2020CL => (1510, 168, 585, 1927),
        // BT.601 (SDTV / Legacy JPEG conversions)
        ffmpeg::color::Space::SMPTE170M | ffmpeg::color::Space::BT470BG => (1436, 352, 731, 1815),
        // BT.709 (HDTV / Modern Web Standard) - Fallback
        _ => (1613, 192, 479, 1900),
    };

    output
        .par_chunks_mut(w * channels)
        .enumerate()
        .for_each(|(y_row, row_out)| {
            for x in 0..w {
                let (cx, cy) = if is_444 {
                    (x, y_row)
                } else if is_422 {
                    (x / 2, y_row)
                } else {
                    (x / 2, y_row / 2)
                };

                let (y_val, cb_val, cr_val) = if is_10bit {
                    let y16 = u16::from_le_bytes([
                        y_data[y_row * y_stride + x * 2],
                        y_data[y_row * y_stride + x * 2 + 1],
                    ]) as i32;
                    let u16v = u16::from_le_bytes([
                        u_data[cy * u_stride + cx * 2],
                        u_data[cy * u_stride + cx * 2 + 1],
                    ]) as i32;
                    let v16v = u16::from_le_bytes([
                        v_data[cy * v_stride + cx * 2],
                        v_data[cy * v_stride + cx * 2 + 1],
                    ]) as i32;
                    (y16, u16v - 512, v16v - 512) // 10-bit range (offset 512)
                } else {
                    let y8 = y_data[y_row * y_stride + x] as i32;
                    let u8v = u_data[cy * u_stride + cx] as i32;
                    let v8v = v_data[cy * v_stride + cx] as i32;
                    (y8, u8v - 128, v8v - 128) // 8-bit range (offset 128)
                };

                let (r, g, b) = if is_10bit {
                    // 10-bit math: shift by 12 (10 for the matrix fraction + 2 for downscaling 10bit->8bit)
                    let r = (y_val * 1024 + c_cr_r * cr_val) >> 12;
                    let g = (y_val * 1024 - c_cb_g * cb_val - c_cr_g * cr_val) >> 12;
                    let b = (y_val * 1024 + c_cb_b * cb_val) >> 12;
                    (r, g, b)
                } else {
                    // 8-bit math: shift by 10
                    let r = (y_val * 1024 + c_cr_r * cr_val) >> 10;
                    let g = (y_val * 1024 - c_cb_g * cb_val - c_cr_g * cr_val) >> 10;
                    let b = (y_val * 1024 + c_cb_b * cb_val) >> 10;
                    (r, g, b)
                };

                let out_off = x * channels;
                // clamp(0, 255) for integers is turned by the compiler into lightning-fast instructions
                row_out[out_off] = r.clamp(0, 255) as u8;
                row_out[out_off + 1] = g.clamp(0, 255) as u8;
                row_out[out_off + 2] = b.clamp(0, 255) as u8;

                if has_alpha {
                    row_out[out_off + 3] = if is_10bit {
                        let a_data_ref = a_data.unwrap();
                        let a_off = y_row * a_stride + x * 2;
                        let a16 = u16::from_le_bytes([a_data_ref[a_off], a_data_ref[a_off + 1]]);
                        (a16 >> 2).min(255) as u8 // Fast downscale 10 -> 8
                    } else {
                        a_data.unwrap()[y_row * a_stride + x]
                    };
                }
            }
        });

    Ok((output, w, h, channels))
}

/// Decode the first frame from one AV1 stream in an AVIF container.
/// Alpha is stored as a second auxiliary AV1 stream, so it cannot be handled
/// by selecting only the container's best video stream.
fn decode_video_stream(
    path: &str,
    stream_idx: usize,
    threads: usize,
) -> PyResult<ffmpeg::frame::Video> {
    let mut options = ffmpeg::Dictionary::new();
    options.set("probesize", "4096");
    options.set("analyzeduration", "0");

    let mut ictx = ffmpeg::format::input_with_dictionary(path, options).map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to open AVIF file {}: {}", path, e))
    })?;

    let parameters = ictx
        .streams()
        .find(|stream| stream.index() == stream_idx)
        .map(|stream| stream.parameters())
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Video stream {} not found",
                stream_idx
            ))
        })?;

    let context = ffmpeg::codec::context::Context::from_parameters(parameters)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    let codec = ffmpeg::decoder::find_by_name("libdav1d")
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("dav1d decoder not found"))?;

    let mut decoder_builder = context.decoder();
    decoder_builder.set_threading(ffmpeg::threading::Config {
        kind: ffmpeg::threading::Type::Slice,
        count: threads,
    });
    let mut decoder = decoder_builder
        .open_as(codec)
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Decoder open failed: {}", e))
        })?
        .video()
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Video context failed: {}", e))
        })?;

    let mut decoded = ffmpeg::frame::Video::empty();
    for (stream, packet) in ictx.packets() {
        if stream.index() != stream_idx || decoder.send_packet(&packet).is_err() {
            continue;
        }
        while decoder.receive_frame(&mut decoded).is_ok() {
            return Ok(decoded);
        }
    }

    if decoder.send_eof().is_ok() {
        while decoder.receive_frame(&mut decoded).is_ok() {
            return Ok(decoded);
        }
    }

    Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
        "Could not decode any frame from AVIF stream {}",
        stream_idx
    )))
}

/// Extract the luma plane of an auxiliary alpha stream and normalize it to
/// the public uint8 representation used by `decode_file`.
fn extract_alpha_plane(decoded: &ffmpeg::frame::Video) -> PyResult<Vec<u8>> {
    let format = decoded.format();
    let (bytes_per_sample, shift, big_endian) = match format {
        Pixel::GRAY8 | Pixel::YUV420P | Pixel::YUV422P | Pixel::YUV444P => (1, 0, false),
        Pixel::GRAY10LE | Pixel::YUV420P10LE | Pixel::YUV422P10LE | Pixel::YUV444P10LE => {
            (2, 2, false)
        }
        Pixel::GRAY10BE | Pixel::YUV420P10BE | Pixel::YUV422P10BE | Pixel::YUV444P10BE => {
            (2, 2, true)
        }
        Pixel::GRAY16LE | Pixel::YUV420P16LE | Pixel::YUV422P16LE | Pixel::YUV444P16LE => {
            (2, 8, false)
        }
        Pixel::GRAY16BE | Pixel::YUV420P16BE | Pixel::YUV422P16BE | Pixel::YUV444P16BE => {
            (2, 8, true)
        }
        other => {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Unsupported alpha pixel format: {:?}",
                other
            )));
        }
    };

    let width = decoded.width() as usize;
    let height = decoded.height() as usize;
    let stride = decoded.stride(0);
    let plane = decoded.data(0);
    let mut alpha = vec![0u8; width * height];

    for y in 0..height {
        let row = &plane[y * stride..];
        for x in 0..width {
            let value = if bytes_per_sample == 1 {
                row[x] as u16
            } else {
                let offset = x * 2;
                if big_endian {
                    u16::from_be_bytes([row[offset], row[offset + 1]])
                } else {
                    u16::from_le_bytes([row[offset], row[offset + 1]])
                }
            };
            alpha[y * width + x] = (value >> shift).min(255) as u8;
        }
    }

    Ok(alpha)
}

/// Decoding of an AVIF image file into raw pixel data.
/// Utilization of the `libdav1d` decoder via FFmpeg for AV1 stream processing.
/// Optimization of stream opening via minimal probe size and analysis duration.
/// Multi-threaded slice decoding controlled by the threads parameter.
/// Parallelized conversion of YUV frames to RGB.
/// Return of a dictionary containing the pixel buffer and image dimensions.
#[pyfunction]
#[pyo3(signature = (path, threads=0))]
fn decode_avif(py: Python<'_>, path: &str, threads: usize) -> PyResult<Py<PyAny>> {
    ensure_ffmpeg_init();
    let path_owned = path.to_string();

    let (data, width, height, channels) =
        py.detach(move || -> PyResult<(Vec<u8>, usize, usize, usize)> {
            let mut options = ffmpeg::Dictionary::new();
            options.set("probesize", "4096");
            options.set("analyzeduration", "0");

            let probe =
                ffmpeg::format::input_with_dictionary(&path_owned, options).map_err(|e| {
                    pyo3::exceptions::PyIOError::new_err(format!(
                        "Failed to open AVIF file {}: {}",
                        path_owned, e
                    ))
                })?;

            let color_stream_idx = probe
                .streams()
                .best(ffmpeg::media::Type::Video)
                .map(|stream| stream.index())
                .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("No video stream found"))?;
            let video_streams: Vec<usize> = probe
                .streams()
                .filter(|stream| stream.parameters().medium() == ffmpeg::media::Type::Video)
                .map(|stream| stream.index())
                .collect();
            drop(probe);

            let color_frame = decode_video_stream(&path_owned, color_stream_idx, threads)?;
            let (mut data, width, height, mut channels) = yuv_to_rgb_parallel(&color_frame)?;

            if channels == 3 {
                for alpha_stream_idx in video_streams {
                    if alpha_stream_idx == color_stream_idx {
                        continue;
                    }

                    let alpha_frame =
                        match decode_video_stream(&path_owned, alpha_stream_idx, threads) {
                            Ok(frame) => frame,
                            Err(_) => continue,
                        };
                    let alpha = match extract_alpha_plane(&alpha_frame) {
                        Ok(alpha)
                            if alpha_frame.width() as usize == width
                                && alpha_frame.height() as usize == height
                                && alpha.len() == width * height =>
                        {
                            alpha
                        }
                        _ => continue,
                    };

                    let mut rgba = Vec::with_capacity(width * height * 4);
                    for (rgb, a) in data.chunks_exact(3).zip(alpha.into_iter()) {
                        rgba.extend_from_slice(rgb);
                        rgba.push(a);
                    }
                    data = rgba;
                    channels = 4;
                    break;
                }
            }

            Ok((data, width, height, channels))
        })?;

    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("data", PyBytes::new(py, &data))?;
    dict.set_item("width", width)?;
    dict.set_item("height", height)?;
    dict.set_item("channels", channels)?;
    Ok(dict.into())
}

#[pymodule]
fn _nvavif_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyColorDepth>()?;
    m.add_class::<PyChroma>()?;
    m.add_class::<PyColorMatrix>()?;
    m.add_function(wrap_pyfunction!(encode_avif, m)?)?;
    m.add_function(wrap_pyfunction!(decode_avif, m)?)?;
    m.add_function(wrap_pyfunction!(is_hardware_supported, m)?)?;
    Ok(())
}
