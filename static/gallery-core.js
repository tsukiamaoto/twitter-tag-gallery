/* Explicit UI extension points, shared requests and one runtime state. */
(() => {
  'use strict';
  const middleware = new Map(), listeners = new Map();
  const runtime = { value: null, pending: null };
  const events = {
    on(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
      return () => listeners.get(name)?.delete(callback);
    },
    emit(name, value) { for (const callback of listeners.get(name) || []) callback(value); }
  };
  async function api(url, options = {}) {
    const response = await fetch(url, options);
    let value;
    try { value = await response.json(); } catch { throw new Error('伺服器回應格式錯誤'); }
    if (!response.ok || ['error', 'failed', 'busy', 'already_running'].includes(value.status)) {
      const reload = response.status === 404 && /^\/api\/(settings|runtime|jobs)/.test(url);
      const error = new Error(reload ? '後端尚未載入新版，請重新啟動圖庫服務。' : typeof value.detail === 'string' ? value.detail : value.message || `操作失敗（${response.status}）`);
      error.status = response.status; throw error;
    }
    return value;
  }
  window.GalleryApp = Object.freeze({
    api, ...events, runtime,
    use(name, handler) {
      if (!middleware.has(name)) middleware.set(name, []);
      middleware.get(name).push(handler);
    },
    run(name, args, base) {
      const chain = middleware.get(name) || [];
      const call = (index, values) => index < chain.length ? chain[index]((...next) => call(index + 1, next), ...values) : base(...values);
      return call(0, args);
    },
    async refreshRuntime() {
      if (runtime.pending) return runtime.pending;
      runtime.pending = api('/api/runtime').catch(async error => {
        if (error.status !== 404) throw error;
        const [ai, reverse, schedule] = await Promise.all([api('/api/ai/status'), api('/api/reverse_search/status'), api('/api/schedule/status')]);
        let active = schedule.background_job || {};
        if (active.name && !active.id) active = { ...active, id: `legacy-${active.pid || 'job'}`, can_stop: false };
        return { ai, reverse, schedule, active_job: active, jobs: [], model: {}, backend_reload_required: true };
      }).then(value => {
        runtime.value = value; events.emit('runtime', value); return value;
      }).finally(() => { runtime.pending = null; });
      return runtime.pending;
    }
  });
})();
