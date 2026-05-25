// PM2 ecosystem for STACK_001.
//
// Single canonical process: the wiseorder orchestrator. Inside this one
// process live the async workers, the FastAPI server, the event watcher,
// and the new IntegrityWatcher + DailySummary scheduler. Splitting into
// multiple PM2 processes is possible but not necessary at v0.1 — the
// asyncio runtime handles internal concurrency.
//
// Bring up:
//   cd ~/Desktop/wiseorder
//   pm2 start ecosystem.config.js
//   pm2 save                       # persist process list for next reboot
//   pm2 startup                    # follow the printed sudo command once
//
// Inspect:
//   pm2 status
//   pm2 logs wiseorder-orchestrator
//   pm2 monit
//
// Stop / restart:
//   pm2 stop wiseorder-orchestrator
//   pm2 restart wiseorder-orchestrator
//   pm2 delete wiseorder-orchestrator
//
// Conventions:
//   - Logs live under ./logs/pm2/  (we don't pollute the app's own
//     logs/wiseorder.jsonl with PM2's own stdout capture)
//   - Auto-restart on exit; cap at 10 restarts/min to surface flapping
//   - Restart at >500 MB RSS — catches memory leaks before they OOM the host
//   - kill_timeout 8s — gives the orchestrator's graceful shutdown time
//     (it has BLPOP-1s workers + uvicorn graceful + watcher join 5s)

module.exports = {
  apps: [
    {
      name: "wiseorder-orchestrator",
      script: ".venv/bin/python",
      args: "-m core.orchestrator.main",
      cwd: __dirname,
      interpreter: "none",  // venv/bin/python is the interpreter; PM2 should exec it directly
      autorestart: true,
      restart_delay: 2000,
      max_restarts: 10,
      min_uptime: "30s",
      max_memory_restart: "500M",
      kill_timeout: 8000,
      wait_ready: false,
      env: {
        PYTHONUNBUFFERED: "1",     // ensure stdout is line-buffered into PM2's log capture
        PYTHONDONTWRITEBYTECODE: "1",
      },
      out_file: "./logs/pm2/orchestrator.out.log",
      error_file: "./logs/pm2/orchestrator.err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
