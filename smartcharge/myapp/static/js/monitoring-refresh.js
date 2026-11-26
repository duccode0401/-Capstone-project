const refresh = document.querySelector('#auto-refresh');
if (refresh) {
  let loading = false;
  setInterval(async () => {
    const results = document.querySelector('#monitoring-results');
    if (loading || document.hidden || !refresh.checked || results.querySelector('details[open]')) return;
    loading = true;
    try {
      const response = await fetch(location.href, {cache: 'no-store'});
      if (!response.ok || response.redirected) return;
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updated = html.querySelector('#monitoring-results');
      if (updated && refresh.checked && !results.querySelector('details[open]') &&
          !results.contains(document.activeElement)) {
        results.replaceWith(updated);
      }
    } catch (_) {
      // Leave the current results visible during a network interruption.
    } finally {
      loading = false;
    }
  }, 10000);
}
