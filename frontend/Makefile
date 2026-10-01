build-Website:
	SWITCHBOARD_AWS=1 NEXT_PUBLIC_AWS_DEPLOYMENT=1 NEXT_PUBLIC_AUTH_MODE=demo NEXT_TELEMETRY_DISABLED=1 npm run build
	cd ../backend && uv run python ../scripts/sam_artifacts.py website "$(ARTIFACTS_DIR)"
