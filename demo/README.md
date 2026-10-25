# Demo video

`demo.mp4`: 2:40, 1920x1080, H.264 + AAC, English voiceover (edge-tts, en-US-AndrewNeural).

Rebuild (dashboard must be running on :8770 against the PayPal sandbox with a local Ollama model, emails masked with `?mask=1`):

```bash
LLM_PROVIDER=ollama PAYOUTPILOT_DATA=/tmp/demo-data uvicorn payoutpilot.api:app --port 8770 &
python demo/build/slides.py        # title / problem / architecture / closing cards -> PNG
python demo/build/record.py        # headless Playwright recordings of the dashboard scenes
python demo/build/assemble.py      # voiceover + captions + cards -> demo/demo.mp4
```

The recordings reuse the existing sandbox batch 8JYSZ8ZNGGT9N; no new payout batch is created
(the approval scene types a wrong total on purpose, which is refused before any PayPal call).
