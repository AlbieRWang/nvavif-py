@echo off
set FFMPEG_DIR=O:\Project\Media\nvavif-py\ffmpeg-out
set LIBCLANG_PATH=O:\Project\Media\nvavif-py\msys64\mingw64\bin
set PKG_CONFIG_PATH=O:\Project\Media\nvavif-py\ffmpeg-out\lib\pkgconfig
set PKG_CONFIG=O:\Project\Media\nvavif-py\msys64\mingw64\bin\pkg-config.exe
set PATH=O:\Project\Media\nvavif-py\msys64\mingw64\bin;O:\Project\Media\nvavif-py\msys64\usr\bin;%PATH%
cd /d O:\Project\Media\nvavif-py
O:\Project\Media\nvavif-py\.venv\Scripts\python.exe -m maturin build --release --interpreter O:\Project\Media\nvavif-py\.venv\Scripts\python.exe %*