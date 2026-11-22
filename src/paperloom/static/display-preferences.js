(() => {
  const sizes = [90, 100, 110, 120, 130, 140];
  const defaultSize = 100;
  const preferences = {
    interface: {
      key: 'paperloom.display.interface-size',
      property: '--interface-font-size',
      cssValue: size => `${size}%`,
    },
    report: {
      key: 'paperloom.display.report-size',
      property: '--report-font-scale',
      cssValue: size => String(size / 100),
    },
  };

  function apply(preference, value) {
    const size = Number(value);
    preference.size = sizes.includes(size) ? size : defaultSize;
    document.documentElement.style.setProperty(preference.property, preference.cssValue(preference.size));
    preference.refreshControl?.();
  }

  let storage;
  try { storage = window.localStorage; } catch { /* Storage can be unavailable. */ }
  for (const preference of Object.values(preferences)) {
    let saved;
    try { saved = storage?.getItem(preference.key); } catch { /* Start at the default. */ }
    apply(preference, saved);
  }

  function connectControls() {
    for (const container of document.querySelectorAll('[data-font-preference]')) {
      const preference = preferences[container.dataset.fontPreference];
      const select = container.querySelector('select');
      const reset = container.querySelector('button');
      select.replaceChildren(...sizes.map(size => new Option(
        size === defaultSize ? '100%（默认）' : `${size}%`, String(size))));
      preference.refreshControl = () => {
        select.value = String(preference.size);
        reset.disabled = preference.size === defaultSize;
      };
      const change = value => {
        apply(preference, value);
        try {
          if (preference.size === defaultSize) storage?.removeItem(preference.key);
          else storage?.setItem(preference.key, String(preference.size));
        } catch { /* The setting can still apply in this tab. */ }
      };
      select.addEventListener('change', () => change(select.value));
      reset.addEventListener('click', () => {
        change(defaultSize);
        select.focus();
      });
      preference.refreshControl();
      container.hidden = false;
    }
  }

  window.addEventListener('storage', event => {
    if (event.storageArea !== storage) return;
    for (const preference of Object.values(preferences)) {
      if (event.key === preference.key || event.key === null) apply(preference, event.newValue);
    }
  });
  document.addEventListener('DOMContentLoaded', connectControls);
})();
