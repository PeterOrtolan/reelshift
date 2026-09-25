# Reelshift — Multi-Platform Content Automation Platform

Automated content scheduling + posting for Instagram (YouTube + TikTok planned).

## Features
- Auto-loop: pull content -> generate FFmpeg variants -> schedule -> post -> auto-trigger next day
- Anti-duplicate variants: zoom, crop, rotation, color shifts, noise, audio EQ
- Multi-account: one bot manages 1-100+ IG accounts
- IG Login OAuth: add accounts via /igconnect
- Source ingestion: Google Drive + Dropbox with dedup
- Auto-resume on container restart
- Telegram bot UI with 30+ commands

## Structure
- app/ — base fork
- app-agency_a/ — agency fork
- app-neneagency/ — neneagency fork
- app-anna2/ — no loop fork
- app-bindilondon/ — vault/library fork with covers + scraping
- elvie-app/ — skip-variants fork
- deployments-template/ — scrubbed config templates

## Setup
1. Clone repo
2. Copy deployments-template/.env.example to .env, fill in credentials
3. Copy deployments-template/configs/bot.json.example to configs/bot.json, fill in token
4. docker compose up -d
5. /igcampaign 3 in Telegram

## Commands
- /igcampaign N — pull N videos, generate variants, schedule, start loop
- /igconnect @user — add IG account via OAuth
- /loopstatus — show loop state
- /stoploop — stop auto-loop
- /igremovebulk @u1 @u2 — bulk remove accounts
