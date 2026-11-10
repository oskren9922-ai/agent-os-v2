@echo off
rem Give the local daemon a public HTTPS address so PayPal can deliver webhooks.
rem See scripts\tunnel.sh for the full steps. Needs ngrok: https://ngrok.com/download
if "%AGENTOS_PORT%"=="" set AGENTOS_PORT=8000
where ngrok >nul 2>nul || (echo ngrok is not installed: https://ngrok.com/download & pause & exit /b 1)
echo Webhook URL will be:  ^<the https URL below^>/webhooks/paypal
ngrok http %AGENTOS_PORT%
