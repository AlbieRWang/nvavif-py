use std::env;
use std::path::PathBuf;

/// Configuration of FFmpeg library and include paths for the build environment.
/// Resolution of binary, library, and header locations via the `FFMPEG_DIR` environment variable.
/// Linker search path registration for the build process.
/// Propagation of path information through environment variables for downstream build stages.
fn main() {
    if let Ok(ffmpeg_dir_str) = env::var("FFMPEG_DIR") {
        let ffmpeg_dir = PathBuf::from(ffmpeg_dir_str);

        let lib_dir = ffmpeg_dir.join("lib");
        let bin_dir = ffmpeg_dir.join("bin");
        let include_dir = ffmpeg_dir.join("include");

        println!("cargo:rustc-link-search=native={}", lib_dir.display());
        println!("cargo:rustc-link-search=native={}", bin_dir.display());

        unsafe {
            env::set_var("FFMPEG_INCLUDE_DIR", include_dir.to_str().unwrap());
            env::set_var("FFMPEG_LIB_DIR", lib_dir.to_str().unwrap());
        }
    }
}
