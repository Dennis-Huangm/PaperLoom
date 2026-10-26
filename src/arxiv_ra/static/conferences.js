(() => {
const {jobs} = window.PaperLoom;

document.querySelectorAll('.conference-search-form').forEach((form) => jobs.bindForm(form));
})();
