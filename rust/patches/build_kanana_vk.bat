@echo off
rem Kanana2Tiny 패치판 llama.cpp — Vulkan DLL 빌드 (llama-cpp-python 0.3.35 휠과 같은 옵션)
call "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat" >nul
set VULKAN_SDK=C:\VulkanSDK\1.4.363.1
set PATH=%VULKAN_SDK%\Bin;%PATH%
set CMAKE="C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
cd /d D:\Project\_build\llama_kanana
rem configure done
%CMAKE% --build build-vk --config Release -j 8 --target llama-perplexity llama-bench || exit /b 1
echo BUILD_OK
