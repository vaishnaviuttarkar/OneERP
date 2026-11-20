(function () {
  var body = document.body;
  // ---- sidebar rail (desktop) ----
  try { if (localStorage.getItem('rail') === '1') body.classList.add('rail'); } catch (e) {}
  var rail = document.getElementById('railBtn');
  if (rail) rail.addEventListener('click', function () {
    body.classList.toggle('rail');
    try { localStorage.setItem('rail', body.classList.contains('rail') ? '1' : '0'); } catch (e) {}
  });
  // ---- dark mode ----
  var themeBtn = document.getElementById('themeBtn');
  function paint() {
    var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    if (themeBtn) themeBtn.innerHTML = '<i class="bi ' + (dark ? 'bi-sun' : 'bi-moon-stars') + '"></i>';
  }
  paint();
  if (themeBtn) themeBtn.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
    paint();
    window.dispatchEvent(new Event('themechange'));
  });
  // ---- auto-dismiss success messages ----
  setTimeout(function () {
    document.querySelectorAll('.alert-success.alert-dismissible').forEach(function (a) {
      a.classList.remove('show'); setTimeout(function () { a.remove(); }, 300);
    });
  }, 5000);
  // ---- command palette (Ctrl/Cmd + K) ----
  var cmdk = document.getElementById('cmdk'); if (!cmdk) return;
  var input = cmdk.querySelector('input'), list = cmdk.querySelector('ul'), items = [], sel = 0;
  var quick = [
    ['New invoice', '/sales/invoices/new/', 'bi-receipt'], ['New challan', '/sales/challans/new/', 'bi-truck'],
    ['New purchase bill', '/purchasing/new/', 'bi-bag-plus'], ['New production batch', '/production/batches/new/', 'bi-plus-circle'],
    ['Add work entry', '/production/entries/new/', 'bi-clipboard2-plus'], ['Issue material', '/production/issue/', 'bi-box-arrow-right'],
    ['New customer receipt', '/finance/receipts/new/', 'bi-cash-coin'], ['New expense', '/finance/expenses/new/', 'bi-credit-card'],
    ['New wage statement', '/payroll/new/', 'bi-wallet2'], ['New party', '/parties/new/', 'bi-person-plus']
  ];
  var pages = [];
  document.querySelectorAll('.sidebar .nav-link').forEach(function (a) {
    pages.push([a.textContent.trim(), a.getAttribute('href'), a.querySelector('i').className.replace('bi ', ''), 'Go to']);
  });
  var allowed = {};
  // quick actions are only offered if the user can open that area (backend still enforces permissions)
  var navHrefs = pages.map(function (p) { return p[1]; }).join(' ');
  var base = quick.filter(function (q) { return navHrefs.indexOf('/' + q[1].split('/')[1] + '/') > -1; }).map(function (q) { return q.concat('Create'); });
  var all = pages.concat(base);
  function render(q) {
    q = (q || '').toLowerCase();
    items = all.filter(function (x) { return x[0].toLowerCase().indexOf(q) > -1; }).slice(0, 12);
    sel = 0;
    list.innerHTML = items.map(function (x, i) {
      return '<li class="' + (i === 0 ? 'sel' : '') + '"><a href="' + x[1] + '"><i class="bi ' + x[2] + '"></i>' + x[0] + '<small>' + x[3] + '</small></a></li>';
    }).join('') || '<li><a href="#" onclick="return false"><i class="bi bi-search"></i>No matches</a></li>';
  }
  function open() { cmdk.classList.add('open'); input.value = ''; render(''); setTimeout(function () { input.focus(); }, 20); }
  function close() { cmdk.classList.remove('open'); }
  document.querySelectorAll('[data-cmdk]').forEach(function (b) { b.addEventListener('click', open); });
  cmdk.addEventListener('click', function (e) { if (e.target === cmdk) close(); });
  input.addEventListener('input', function () { render(input.value); });
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); cmdk.classList.contains('open') ? close() : open(); return; }
    if (!cmdk.classList.contains('open')) return;
    var lis = list.querySelectorAll('li');
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); if (!lis.length) return;
      lis[sel].classList.remove('sel'); sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + lis.length) % lis.length;
      lis[sel].classList.add('sel'); lis[sel].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && items[sel]) { window.location = items[sel][1]; }
  });
})();
