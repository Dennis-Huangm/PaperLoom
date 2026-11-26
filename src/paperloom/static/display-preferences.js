(() => {
  const fontOptions = new Map([90, 100, 110, 120, 130, 140].map(size => [
    String(size), size === 100 ? '100%（默认）' : `${size}%`,
  ]));
  const defaultReportWidth = '1200';
  const preferences = {
    interface: {
      key: 'paperloom.display.interface-size',
      property: '--interface-font-size',
      options: fontOptions,
      defaultValue: '100',
      cssValue: size => `${size}%`,
    },
    report: {
      key: 'paperloom.display.report-size',
      property: '--report-font-scale',
      options: fontOptions,
      defaultValue: '100',
      cssValue: size => String(size / 100),
    },
    reportWidth: {
      key: 'paperloom.display.report-width',
      property: '--report-reading-width',
      options: new Map([[defaultReportWidth, `${defaultReportWidth} px（默认）`]]),
      defaultValue: defaultReportWidth,
      min: 600,
      max: 2560,
      step: 20,
      cssValue: width => `${width}px`,
    },
  };

  function apply(preference, value) {
    const valid = preference.min === undefined ? preference.options.has(value)
      : /^\d+$/.test(value) && Number(value) >= preference.min && Number(value) <= preference.max
        && (Number(value) - preference.min) % preference.step === 0;
    preference.value = valid ? value : preference.defaultValue;
    document.documentElement.style.setProperty(preference.property, preference.cssValue(preference.value));
    preference.refreshControl?.();
  }

  let storage;
  try { storage = window.localStorage; } catch { /* Storage can be unavailable. */ }
  for (const preference of Object.values(preferences)) {
    let saved;
    try { saved = storage?.getItem(preference.key); } catch { /* Start at the default. */ }
    apply(preference, saved);
  }

  function save(preference, value) {
    apply(preference, value);
    try {
      if (preference.value === preference.defaultValue) storage?.removeItem(preference.key);
      else storage?.setItem(preference.key, preference.value);
    } catch { /* The setting can still apply in this tab. */ }
  }

  function connectSelect(container, preference) {
    const select = container.querySelector('select');
    const reset = container.querySelector('button');
    select.replaceChildren(...Array.from(preference.options, ([value, label]) => new Option(label, value)));
    preference.refreshControl = () => {
      // Older report exports can still display a saved custom width in their dropdown.
      select.querySelector('[data-custom-value]')?.remove();
      if (!preference.options.has(preference.value)) {
        const option = new Option(`${preference.value} px`, preference.value);
        option.dataset.customValue = '';
        select.add(option);
      }
      select.value = preference.value;
      if (reset) reset.disabled = preference.value === preference.defaultValue;
    };
    select.addEventListener('change', () => save(preference, select.value));
    reset?.addEventListener('click', () => {
      save(preference, preference.defaultValue);
      select.focus();
    });
  }

  function connectRange(container, slider, preference) {
    slider.min = preference.min;
    slider.max = preference.max;
    slider.step = preference.step;
    const panel = slider.closest('[popover]');
    panel?.addEventListener('keydown', event => {
      // Let the browser dismiss the popover before report sidebars handle Escape.
      if (event.key === 'Escape') event.stopPropagation();
    });
    const output = container.querySelector('output');
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.textContent = '恢复默认';
    reset.addEventListener('click', () => save(preference, preference.defaultValue));
    container.querySelector('.report-width-presets').append(reset);
    preference.refreshControl = () => {
      slider.value = preference.value;
      output.textContent = `${preference.value} px`;
      const label = preference.value === preference.defaultValue ? '，默认' : '';
      slider.setAttribute('aria-valuetext', `${preference.value} 像素${label}`);
    };
    slider.addEventListener('input', () => apply(preference, slider.value));
    slider.addEventListener('change', () => save(preference, slider.value));
  }

  function connectControls() {
    // Previously exported reports still use data-font-preference.
    for (const container of document.querySelectorAll('[data-font-preference], [data-display-preference]')) {
      const preference = preferences[container.dataset.displayPreference || container.dataset.fontPreference];
      const slider = container.querySelector('input[type="range"]');
      if (slider) connectRange(container, slider, preference);
      else connectSelect(container, preference);
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
