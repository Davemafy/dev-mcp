FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY auth.py machine.py browser.py host_access.py entrypoint.py main.py backends.py shared_state.py capabilities.py browser_journey.py browser_proxy.py stream_browser.py stream_browser_server.py stream_browser_control.py adaptive_browser.py observation_delta.py progress_guard.py desktop_manager.py desktop_gateway.py desktop_runtime.py agent_jobs.py workspace_host_access.py ./
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data
# Railway mounts volumes owned by root. entrypoint.py prepares /data once,
# then drops all process privileges to the existing non-root appuser.
EXPOSE 8000
CMD ["python", "entrypoint.py"]
