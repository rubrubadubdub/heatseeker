@echo off
rem Heatseeker launcher — double-click to run.
rem Starts the worker + web GUI in one window and opens your browser.
title Heatseeker
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv is not installed or not on PATH.
    echo Install it from https://docs.astral.sh/uv/ then run this again.
    pause
    exit /b 1
)

uv run heatseeker run
if errorlevel 1 pause
