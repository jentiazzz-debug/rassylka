/* Логика мини-аппа: состояние, экраны, подключение аккаунта.
 *
 * Кто открыл приложение, страница не решает и не сообщает: она лишь
 * пересылает серверу initData — подписанную клиентом Telegram строку.
 * Сервер проверяет подпись токеном бота и берёт user_id оттуда. Поэтому
 * в запросах здесь нет ни одного «свой id», и подделать чужой нельзя.
 */

'use strict';

const tg = window.Telegram && window.Telegram.WebApp;

/** Состояние с сервера: подписка, аккаунты, лимиты. */
let state = null;
/** Незавершённый вход: токен и шаг. */
let login = null;

const $ = (id) => document.getElementById(id);

const SCREENS = [
  'loading', 'main', 'phone', 'code', 'password', 'done', 'tdata', 'new',
  'edit', 'log', 'outside',
];

/** Разделы главного экрана. */
const PANES = ['profile', 'mail', 'accounts'];
let pane = 'profile';

/** Экраны входа по номеру: с них «назад» отменяет вход на сервере. */
const LOGIN_SCREENS = ['phone', 'code', 'password'];

let current = 'loading';

function show(name) {
  current = name;
  for (const screen of SCREENS) {
    $('screen-' + screen).hidden = screen !== name;
  }
  // Кнопка «назад» в шапке Telegram: на вложенных экранах она уводит на
  // главный, на главном её быть не должно — иначе она закрывает
  // приложение, и это выглядит как сбой.
  if (tg && tg.BackButton) {
    const nested = name !== 'main' && name !== 'loading' && name !== 'outside';
    nested ? tg.BackButton.show() : tg.BackButton.hide();
  }
  // Панель разделов — только на главном. На шагах входа и создания
  // рассылки она увела бы человека из середины заполнения формы.
  $('tabbar').hidden = name !== 'main';
  document.body.classList.toggle('with-tabs', name === 'main');
  window.scrollTo(0, 0);
}

function setPane(name) {
  pane = name;
  for (const item of PANES) $('pane-' + item).hidden = item !== name;
  for (const button of document.querySelectorAll('.tab-item')) {
    button.classList.toggle('active', button.dataset.pane === name);
  }
  window.scrollTo(0, 0);
}

/** «Назад» из любого места. С экранов входа — с отменой на сервере:
 *  незавершённый вход держит подключение к Telegram, и бросать его
 *  висеть до истечения таймаута незачем. */
async function goBack() {
  if (LOGIN_SCREENS.includes(current)) {
    await cancelFlow();
    return;
  }
  await refresh();
}

function haptic(kind) {
  if (tg && tg.HapticFeedback) {
    if (kind === 'error' || kind === 'success') {
      tg.HapticFeedback.notificationOccurred(kind);
    } else {
      tg.HapticFeedback.impactOccurred('light');
    }
  }
}

function fail(id, text) {
  const box = $(id);
  box.textContent = text;
  box.hidden = !text;
  if (text) haptic('error');
}

/** Запрос к своему API. initData уходит и заголовком, и в теле:
 *  некоторые прокси хостингов срезают нестандартные заголовки. */
async function api(path, body) {
  const initData = (tg && tg.initData) || '';
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Init-Data': initData,
    },
    body: JSON.stringify(Object.assign({ init_data: initData }, body || {})),
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    return { ok: false, error: 'Сервер не ответил. Попробуйте позже.' };
  }
  if (response.status === 401) {
    return { ok: false, error: data.error || 'Откройте приложение из бота заново.' };
  }
  return data;
}

/** Кнопка на время запроса: заблокировать и сказать, что идёт работа. */
function busy(button, text) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => {
    button.disabled = false;
    button.textContent = button.dataset.label;
  };
}

// --- главный экран ----------------------------------------------------

function dateText(stamp) {
  if (!stamp) return '';
  return new Date(stamp * 1000).toLocaleDateString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
  });
}

function plural(n, one, few, many) {
  const tail = Math.abs(n) % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = tail % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

function renderSubscription() {
  const sub = state.subscription;
  const card = $('sub-card');
  const days = sub.days_left;
  card.classList.toggle('expired', !sub.active);

  if (sub.kind === 'paid') {
    $('sub-title').textContent = '💎 Подписка активна';
    $('sub-note').textContent =
      `До ${dateText(sub.until)}. Подпись о боте в сообщениях не добавляется.`;
  } else if (sub.kind === 'trial') {
    $('sub-title').textContent = '🎁 Пробный период';
    $('sub-note').textContent =
      `Осталось ${days} ${plural(days, 'день', 'дня', 'дней')}, до ` +
      `${dateText(sub.until)}. Пока он идёт, в конце каждого сообщения ` +
      'рассылки дописывается строка о том, каким ботом она сделана.';
  } else {
    $('sub-title').textContent = '⌛️ Бесплатный период закончился';
    $('sub-note').textContent =
      'Аккаунты остаются подключёнными. Чтобы продолжить рассылки, ' +
      'напишите в поддержку — включим подписку.';
  }
}

function accountRow(account) {
  const alive = account.status === 'ok';
  const row = document.createElement('div');
  row.className = 'account';

  const top = document.createElement('div');
  top.className = 'account-top';

  const name = document.createElement('span');
  name.className = 'account-name';
  name.textContent = account.name || account.phone;
  top.appendChild(name);

  const badge = document.createElement('span');
  badge.className = 'badge' + (alive ? '' : ' dead');
  badge.textContent = alive ? 'подключён' : 'не работает';
  top.appendChild(badge);

  const phone = document.createElement('span');
  phone.className = 'account-phone';
  phone.textContent = (account.username ? '@' + account.username : account.phone)
    + (account.source === 'tdata' ? ' · tdata' : '');
  top.appendChild(phone);

  row.appendChild(top);

  if (!alive && account.note) {
    const note = document.createElement('p');
    note.className = 'hint small';
    note.textContent = account.note + '. Подключите аккаунт заново.';
    row.appendChild(note);
  }

  const actions = document.createElement('div');
  actions.className = 'account-actions';

  const check = document.createElement('button');
  check.className = 'btn small';
  check.textContent = 'Проверить';
  check.onclick = async () => {
    const done = busy(check, 'Проверяем…');
    const result = await api('/api/account/verify', { id: account.id });
    done();
    if (!result.ok) {
      alertBox(result.error);
      return;
    }
    haptic(result.account.status === 'ok' ? 'success' : 'error');
    await refresh();
  };
  actions.appendChild(check);

  const forget = document.createElement('button');
  forget.className = 'btn small danger';
  forget.textContent = 'Отключить';
  forget.onclick = () => {
    confirmBox(
      `Отключить ${account.name || account.phone}? Сессия будет отозвана ` +
      'в Telegram, рассылки с этого аккаунта прекратятся.',
      async (yes) => {
        if (!yes) return;
        const done = busy(forget, 'Отключаем…');
        const result = await api('/api/account/forget', { id: account.id });
        done();
        if (!result.ok) {
          alertBox(result.error);
          return;
        }
        haptic('success');
        await refresh();
      },
    );
  };
  actions.appendChild(forget);

  row.appendChild(actions);
  return row;
}

function renderAccounts() {
  const box = $('accounts');
  box.textContent = '';
  for (const account of state.accounts) box.appendChild(accountRow(account));
  $('accounts-empty').hidden = state.accounts.length > 0;

  const full = state.accounts.length >= state.limits.max_accounts;
  const button = $('add-account');
  button.disabled = full || !state.mtproto_ready;
  // Загрузку tdata прячем совсем, когда библиотека разбора не стоит на
  // сервере: кнопка, которая всегда отвечает «не настроено», только
  // мешает.
  const tdataButton = $('add-tdata');
  tdataButton.hidden = !state.tdata_ready;
  tdataButton.disabled = full;
  const note = $('limit-note');
  if (!state.mtproto_ready) {
    note.textContent =
      'Подключение аккаунтов пока не настроено на сервере — напишите в поддержку.';
    note.hidden = false;
  } else if (full) {
    note.textContent =
      `Подключено ${state.accounts.length} из ${state.limits.max_accounts}. ` +
      'Отключите ненужный, чтобы добавить новый.';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

// --- рассылки на главном экране ---------------------------------------

function human(seconds) {
  if (seconds < 60) return seconds + ' сек';
  if (seconds < 3600) return Math.round(seconds / 60) + ' мин';
  const hours = seconds / 3600;
  return (Number.isInteger(hours) ? hours : hours.toFixed(1)) + ' ч';
}

function untilText(stamp) {
  const left = stamp - Math.floor(Date.now() / 1000);
  if (left <= 0) return 'вот-вот';
  return 'через ' + human(left);
}

const STATUS = {
  running: { label: 'идёт', css: '' },
  paused: { label: 'на паузе', css: 'paused' },
  stopped: { label: 'остановлена', css: 'stopped' },
};

function campaignCard(campaign) {
  const row = document.createElement('div');
  row.className = 'campaign';

  const top = document.createElement('div');
  top.className = 'campaign-top';
  const name = document.createElement('span');
  name.className = 'campaign-name';
  name.textContent = campaign.title;
  top.appendChild(name);

  const status = STATUS[campaign.status] || STATUS.stopped;
  const badge = document.createElement('span');
  badge.className = 'badge ' + status.css;
  badge.textContent = status.label;
  top.appendChild(badge);
  row.appendChild(top);

  const text = document.createElement('div');
  text.className = 'campaign-text';
  // У рассылки материалом текст в карточке обманчив: уйдёт не он, а
  // сообщение из «Избранного» — с медиа и оформлением.
  text.textContent = campaign.content === 'saved'
    ? '📎 Сообщение из «Избранного» — с медиа и оформлением'
    : campaign.text;
  row.appendChild(text);

  const facts = document.createElement('div');
  facts.className = 'campaign-facts';
  const parts = [
    `${campaign.chats} ${plural(campaign.chats, 'чат', 'чата', 'чатов')}`,
    `интервал ${human(campaign.interval)}`,
    `отправлено ${campaign.sent_ok}`,
  ];
  if (campaign.sent_err) parts.push(`ошибок ${campaign.sent_err}`);
  if (campaign.cycles) parts.push(`кругов ${campaign.cycles}`);
  if (campaign.status === 'running') {
    parts.push('следующее ' + untilText(campaign.next_run_at));
  }
  facts.textContent = parts.join(' · ');
  row.appendChild(facts);

  if (campaign.note) {
    const note = document.createElement('div');
    note.className = 'campaign-facts';
    note.textContent = '⚠️ ' + campaign.note;
    row.appendChild(note);
  }

  const actions = document.createElement('div');
  actions.className = 'campaign-actions';

  const toggle = document.createElement('button');
  toggle.className = 'btn small';
  toggle.textContent = campaign.status === 'running' ? 'Пауза' : 'Продолжить';
  toggle.onclick = async () => {
    const done = busy(toggle, '…');
    const result = await api('/api/campaign/toggle', { id: campaign.id });
    done();
    if (!result.ok) {
      alertBox(result.error);
      return;
    }
    haptic('light');
    await refresh();
  };
  actions.appendChild(toggle);

  const change = document.createElement('button');
  change.className = 'btn small';
  change.textContent = 'Изменить';
  change.onclick = () => openEdit(campaign);
  actions.appendChild(change);

  const journal = document.createElement('button');
  journal.className = 'btn small';
  journal.textContent = 'Журнал';
  journal.onclick = () => openLog(campaign);
  actions.appendChild(journal);

  const remove = document.createElement('button');
  remove.className = 'btn small danger';
  remove.textContent = 'Удалить';
  remove.onclick = () => {
    confirmBox(`Удалить рассылку «${campaign.title}»?`, async (yes) => {
      if (!yes) return;
      const result = await api('/api/campaign/delete', { id: campaign.id });
      if (!result.ok) {
        alertBox(result.error);
        return;
      }
      haptic('success');
      await refresh();
    });
  };
  actions.appendChild(remove);

  row.appendChild(actions);
  return row;
}

function renderCampaigns() {
  const box = $('campaigns');
  box.textContent = '';
  for (const campaign of state.campaigns) box.appendChild(campaignCard(campaign));
  $('campaigns-empty').hidden = state.campaigns.length > 0;

  // Рассылать не с чего, пока нет живого аккаунта: кнопка есть, но
  // объясняет, чего не хватает, — так понятнее, чем спрятанная кнопка.
  const alive = state.accounts.filter((a) => a.status === 'ok');
  const button = $('add-campaign');
  const note = $('campaign-note');
  button.disabled = alive.length === 0 || !state.subscription.active;
  if (alive.length === 0) {
    note.textContent = 'Сначала подключите аккаунт — рассылка идёт от его имени.';
    note.hidden = false;
  } else if (!state.subscription.active) {
    note.textContent = 'Бесплатный период закончился — новые рассылки не создаются.';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

// --- профиль -----------------------------------------------------------

/** Профиль приезжает отдельным запросом: аватарка и имя берутся из
 *  подписанной initData, а не из базы, и обновлять их вместе с
 *  состоянием рассылок незачем. */
let me = null;

function renderProfile() {
  if (!me) return;

  const photo = $('me-photo');
  const letter = $('me-letter');
  if (me.photo_url) {
    photo.src = me.photo_url;
    photo.hidden = false;
    letter.hidden = true;
  } else {
    // У закрытых профилей Telegram аватарку не отдаёт — рисуем букву.
    photo.hidden = true;
    letter.hidden = false;
    letter.textContent = (me.name || '?').trim().charAt(0).toUpperCase();
  }

  $('me-name').textContent = me.name + (me.is_premium ? ' ⭐️' : '');
  $('me-nick').textContent = me.username ? '@' + me.username : 'без ника';
  $('me-id').textContent = 'ID: ' + me.id;

  $('coins-value').textContent = me.stats.coins || 0;
  $('coins-label').textContent = me.coin_name + ' — внутренние монеты';

  const stats = $('me-stats');
  stats.textContent = '';
  const rows = [
    ['Аккаунтов', me.stats.accounts || 0],
    ['Рассылок', me.stats.campaigns || 0],
    ['Сообщений отправлено', me.stats.sent || 0],
    ['Звёзд потрачено', me.stats.stars || 0],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement('div');
    row.className = 'stat-row';
    const left = document.createElement('span');
    left.textContent = label;
    const right = document.createElement('span');
    right.textContent = value;
    row.append(left, right);
    stats.appendChild(row);
  }

  const support = $('me-support');
  support.hidden = !state.support_url;
  if (state.support_url) support.href = state.support_url;

  renderPlans();
}

function renderPlans() {
  const box = $('plans');
  box.textContent = '';
  for (const plan of me.plans) {
    const button = document.createElement('button');
    button.className = 'plan';

    const name = document.createElement('span');
    name.className = 'plan-name';
    name.textContent = plan.days >= 30
      ? 'Месяц'
      : `${plan.days} ${plural(plan.days, 'день', 'дня', 'дней')}`;

    const perDay = document.createElement('span');
    perDay.className = 'plan-day';
    perDay.textContent = plan.days > 1
      ? `${Math.round(plan.stars / plan.days)} ⭐️/день`
      : '';

    const price = document.createElement('span');
    price.className = 'plan-price';
    price.textContent = plan.stars + ' ⭐️';

    button.append(name, perDay, price);
    button.onclick = () => buy(plan, button);
    box.appendChild(button);
  }
}

async function buy(plan, button) {
  const done = busy(button, 'Открываем счёт…');
  const result = await api('/api/invoice', { days: plan.days });
  done();
  if (!result.ok) {
    alertBox(result.error);
    return;
  }
  if (!tg || !tg.openInvoice) {
    alertBox('Ваша версия Telegram не умеет открывать счёт. Обновите приложение.');
    return;
  }
  // Ответ openInvoice — это статус в браузере, и подписку по нему никто
  // не продлевает: настоящее подтверждение приходит боту от Telegram
  // отдельным апдейтом. Здесь только показываем, что произошло, и
  // перечитываем состояние.
  tg.openInvoice(result.link, async (status) => {
    if (status === 'paid') {
      haptic('success');
      // Платёж доезжает до бота не мгновенно — даём ему секунду.
      setTimeout(refresh, 1200);
    } else if (status === 'failed') {
      alertBox('Оплата не прошла.');
    }
  });
}

function renderMain() {
  renderSubscription();
  renderAccounts();
  renderCampaigns();
  renderProfile();
  setPane(pane);
  show('main');
}

// --- правка рассылки ---------------------------------------------------

/** Что правим. Чаты сюда не входят: круг идёт, и менять список на ходу
 *  значит написать в одни чаты дважды, а другие пропустить. */
let editing = null;

async function openEdit(campaign) {
  editing = {
    id: campaign.id,
    accountId: campaign.account_id,
    content: campaign.content || 'text',
    savedId: campaign.saved_id,
    interval: campaign.interval,
  };
  fail('edit-err', '');
  $('edit-about').textContent =
    `${campaign.chats} ${plural(campaign.chats, 'чат', 'чата', 'чатов')} · ` +
    'список чатов не меняется — круг уже идёт';
  $('edit-title').value = campaign.title;
  $('edit-text').value = campaign.text || '';
  $('edit-count').textContent =
    `${(campaign.text || '').length} из ${state.limits.max_text}`;
  setEditTab(editing.content);
  renderEditIntervals();
  show('edit');

  hintInto($('edit-materials'), 'Загружаем…');
  await loadMaterials(editing.accountId);
  renderMaterials($('edit-materials'), editing.savedId, (id) => {
    editing.savedId = id;
  });
}

function setEditTab(name) {
  if (!editing) return;
  editing.content = name;
  for (const tab of document.querySelectorAll('.etab')) {
    tab.classList.toggle('active', tab.dataset.etab === name);
  }
  $('etab-text').hidden = name !== 'text';
  $('etab-saved').hidden = name !== 'saved';
}

function renderEditIntervals() {
  const box = $('edit-intervals');
  box.textContent = '';
  const allowed = INTERVALS.filter((s) => s >= state.limits.min_interval);
  // Свой интервал, выставленный когда-то другими значениями, не теряем:
  // иначе правка названия молча переставила бы рассылку на другой ритм.
  if (!allowed.includes(editing.interval)) allowed.unshift(editing.interval);
  for (const seconds of allowed) {
    const chip = document.createElement('button');
    chip.className = 'chip' + (seconds === editing.interval ? ' active' : '');
    chip.textContent = human(seconds);
    chip.onclick = () => {
      editing.interval = seconds;
      renderEditIntervals();
    };
    box.appendChild(chip);
  }
}

async function saveEdit() {
  const button = $('edit-save');
  fail('edit-err', '');
  const text = $('edit-text').value.trim();
  if (editing.content === 'text' && !text) {
    fail('edit-err', 'Напишите текст сообщения.');
    return;
  }
  if (editing.content === 'saved' && !editing.savedId) {
    fail('edit-err', 'Выберите сообщение из «Избранного».');
    return;
  }
  const done = busy(button, 'Сохраняем…');
  const result = await api('/api/campaign/edit', {
    id: editing.id,
    title: $('edit-title').value.trim(),
    text,
    interval: editing.interval,
    content: editing.content,
    saved_id: editing.savedId,
  });
  done();
  if (!result.ok) {
    fail('edit-err', result.error);
    return;
  }
  haptic('success');
  await refresh();
}

// --- журнал отправок ---------------------------------------------------

async function openLog(campaign) {
  $('log-title').textContent = 'Журнал: ' + campaign.title;
  const box = $('log-list');
  box.textContent = '';
  $('log-empty').hidden = true;
  show('log');

  const result = await api('/api/campaign/log', { id: campaign.id });
  if (!result.ok) {
    alertBox(result.error);
    await refresh();
    return;
  }
  if (!result.sends.length) {
    $('log-empty').hidden = false;
    return;
  }
  for (const send of result.sends) {
    const row = document.createElement('div');
    row.className = 'log-row';

    const mark = document.createElement('span');
    mark.className = 'log-mark ' + (send.ok ? 'good' : 'bad');
    mark.textContent = send.ok ? '✓' : '✕';
    row.appendChild(mark);

    const body = document.createElement('span');
    body.className = 'log-chat';
    body.textContent = send.title || String(send.chat_id);
    if (!send.ok && send.error) {
      const error = document.createElement('div');
      error.className = 'log-error';
      error.textContent = send.error;
      body.appendChild(error);
    }
    row.appendChild(body);

    const when = document.createElement('span');
    when.className = 'log-when';
    when.textContent = new Date(send.created_at * 1000).toLocaleTimeString('ru-RU', {
      hour: '2-digit', minute: '2-digit',
    });
    row.appendChild(when);

    box.appendChild(row);
  }
}

async function refresh() {
  const [result, profile] = await Promise.all([
    api('/api/state', {}),
    api('/api/profile', {}),
  ]);
  if (!result.ok) {
    show('outside');
    $('screen-outside').querySelector('.hint').textContent = result.error;
    return;
  }
  state = result;
  if (profile.ok) me = profile;
  login = result.pending || null;
  renderMain();
}

// --- импорт из tdata ---------------------------------------------------

function openTdata() {
  fail('tdata-err', '');
  $('tdata-note').hidden = true;
  $('tdata-file').value = '';
  $('tdata-passcode').value = '';
  show('tdata');
}

async function uploadTdata() {
  const button = $('tdata-send');
  fail('tdata-err', '');
  $('tdata-note').hidden = true;

  const file = $('tdata-file').files[0];
  if (!file) {
    fail('tdata-err', 'Прикрепите zip-архив с папкой tdata.');
    return;
  }
  // Размер проверяем и здесь: гнать на сервер сто мегабайт, чтобы он
  // ответил отказом, — это минуты ожидания на мобильном интернете.
  const limit = (state.max_tdata_mb || 64) * 1024 * 1024;
  if (file.size > limit) {
    fail('tdata-err', `Архив больше ${state.max_tdata_mb} МБ.`);
    return;
  }

  const body = new FormData();
  body.append('file', file);
  body.append('passcode', $('tdata-passcode').value);

  const done = busy(button, 'Загружаем…');
  let result;
  try {
    // Свой fetch, не общий api(): тело здесь multipart, и подпись
    // приходится класть в заголовок — в JSON её не подмешать.
    const response = await fetch('/api/account/tdata', {
      method: 'POST',
      headers: { 'X-Init-Data': (tg && tg.initData) || '' },
      body,
    });
    result = await response.json();
  } catch (error) {
    result = { ok: false, error: 'Файл не дошёл. Попробуйте ещё раз.' };
  }
  done();
  // Код-пароль в поле не оставляем: приложение сворачивают, а не
  // закрывают, и он лежал бы там до перезагрузки страницы.
  $('tdata-passcode').value = '';

  if (!result.ok) {
    fail('tdata-err', result.error);
    return;
  }

  haptic('success');
  const names = result.added.map((a) => a.name).join(', ');
  const failed = (result.failed || []).length;
  $('done-who').textContent = names
    + (failed ? ` · не вышло: ${failed}` : '');
  show('done');
}

// --- создание рассылки -------------------------------------------------

/** Готовые интервалы, секунды. Мельче минимума сервера отсеиваются:
 *  предлагать кнопку, на которую сервер ответит отказом, — плохой тон. */
const INTERVALS = [60, 300, 900, 1800, 3600, 10800, 21600];

const KINDS = { user: 'личка', group: 'группа', channel: 'канал' };

/** Значок типа материала — по нему список читается с одного взгляда. */
const MATERIAL_ICONS = {
  text: '📝', photo: '🖼', video: '🎬', gif: '🎞',
  sticker: '🎨', audio: '🎧', document: '📎',
};

let draft = null;
let picker = { chats: [], folders: [], materials: [] };

/** Общий список материалов: используется и при создании, и при правке. */
function renderMaterials(box, selectedId, onPick) {
  box.textContent = '';
  if (!picker.materials.length) {
    hintInto(box, 'В «Избранном» этого аккаунта пока пусто. Отправьте туда ' +
      'сообщение и нажмите «Обновить список чатов».');
    return;
  }
  for (const material of picker.materials) {
    const row = document.createElement('label');
    row.className = 'pick';

    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = box.id;
    radio.checked = selectedId === material.msg_id;
    radio.onchange = () => onPick(material.msg_id);
    row.appendChild(radio);

    const mark = document.createElement('span');
    mark.className = 'pick-mark';
    mark.textContent = MATERIAL_ICONS[material.kind] || '📝';
    row.appendChild(mark);

    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = material.preview || 'Сообщение';
    row.appendChild(title);

    box.appendChild(row);
  }
}

async function loadMaterials(accountId) {
  const result = await api('/api/materials', { account_id: accountId });
  picker.materials = result.ok ? result.materials : [];
}

function openNew() {
  const alive = state.accounts.filter((a) => a.status === 'ok');
  if (!alive.length) return;

  draft = {
    accountId: alive[0].id,
    source: 'chats',
    chatIds: new Set(),
    folderId: null,
    interval: 900,
    content: 'text',
    savedId: null,
  };
  $('campaign-text').value = '';
  $('chat-search').value = '';
  fail('new-err', '');
  $('scan-note').hidden = true;

  // Аккаунт спрашиваем, только когда их несколько: выбор из одного
  // пункта — лишний вопрос на экране, где и так много полей.
  const select = $('campaign-account');
  select.textContent = '';
  for (const account of alive) {
    const option = document.createElement('option');
    option.value = String(account.id);
    option.textContent = account.name || account.phone;
    select.appendChild(option);
  }
  select.value = String(draft.accountId);
  $('account-pick').hidden = alive.length < 2;

  renderIntervals();
  renderFooterNote();
  countText();
  setTab('chats');
  setContentTab('text');
  show('new');
  loadChats();
}

function setTab(name) {
  draft.source = name;
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('active', tab.dataset.tab === name);
  }
  $('tab-chats').hidden = name !== 'chats';
  $('tab-folders').hidden = name !== 'folders';
}

function hintInto(box, text) {
  box.textContent = '';
  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.style.padding = '10px';
  hint.textContent = text;
  box.appendChild(hint);
}

async function loadChats() {
  hintInto($('chat-list'), 'Загружаем…');
  const result = await api('/api/chats', { account_id: draft.accountId });
  if (!result.ok) {
    hintInto($('chat-list'), result.error);
    return;
  }
  picker.chats = result.chats;
  picker.folders = result.folders;
  renderChatList();
  renderFolders();
  await loadMaterials(draft.accountId);
  renderMaterials($('material-list'), draft.savedId, (id) => {
    draft.savedId = id;
  });
}

function setContentTab(name) {
  draft.content = name;
  for (const tab of document.querySelectorAll('.ctab')) {
    tab.classList.toggle('active', tab.dataset.ctab === name);
  }
  $('ctab-text').hidden = name !== 'text';
  $('ctab-saved').hidden = name !== 'saved';
}

function renderChatList() {
  const box = $('chat-list');
  const query = $('chat-search').value.trim().toLowerCase();
  box.textContent = '';

  if (!picker.chats.length) {
    hintInto(box, 'Список пуст. Нажмите «Обновить список чатов» — ' +
      'мы прочитаем диалоги этого аккаунта.');
    return;
  }

  const rows = picker.chats.filter((chat) => !query
    || chat.title.toLowerCase().includes(query)
    || (chat.username || '').toLowerCase().includes(query));

  if (!rows.length) {
    hintInto(box, 'Ничего не нашлось.');
    return;
  }

  for (const chat of rows) {
    const row = document.createElement('label');
    row.className = 'pick';

    const box2 = document.createElement('input');
    box2.type = 'checkbox';
    box2.checked = draft.chatIds.has(chat.id);
    box2.onchange = () => {
      box2.checked ? draft.chatIds.add(chat.id) : draft.chatIds.delete(chat.id);
      countPicked();
    };
    row.appendChild(box2);

    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = chat.title;
    row.appendChild(title);

    const kind = document.createElement('span');
    kind.className = 'pick-kind';
    kind.textContent = KINDS[chat.kind] || '';
    row.appendChild(kind);

    box.appendChild(row);
  }
  countPicked();
}

function countPicked() {
  const count = draft.chatIds.size;
  $('chat-picked').textContent =
    `Выбрано: ${count}` + (count ? ` · круг займёт ${human(count * draft.interval)}` : '');
}

function renderFolders() {
  const box = $('folder-list');
  box.textContent = '';
  if (!picker.folders.length) {
    hintInto(box, 'Папок нет. Их создают в Telegram: Настройки → Папки с чатами.');
    return;
  }
  for (const folder of picker.folders) {
    const row = document.createElement('label');
    row.className = 'pick';

    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'folder';
    radio.checked = draft.folderId === folder.id;
    radio.onchange = () => { draft.folderId = folder.id; };
    row.appendChild(radio);

    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = folder.title;
    row.appendChild(title);

    const count = document.createElement('span');
    count.className = 'pick-kind';
    count.textContent = `${folder.chats} ${plural(folder.chats, 'чат', 'чата', 'чатов')}`;
    row.appendChild(count);

    box.appendChild(row);
  }
}

function renderIntervals() {
  const box = $('intervals');
  box.textContent = '';
  const allowed = INTERVALS.filter((s) => s >= state.limits.min_interval);
  if (!allowed.includes(draft.interval)) draft.interval = allowed[0];

  for (const seconds of allowed) {
    const chip = document.createElement('button');
    chip.className = 'chip' + (seconds === draft.interval ? ' active' : '');
    chip.textContent = human(seconds);
    chip.onclick = () => {
      draft.interval = seconds;
      renderIntervals();
      countPicked();
    };
    box.appendChild(chip);
  }
  $('interval-note').textContent =
    `Пауза между сообщениями. Меньше ${human(state.limits.min_interval)} нельзя: ` +
    'за частую отправку Telegram ограничивает аккаунт. ' +
    `Потолок — ${state.limits.daily_limit} сообщений в сутки на аккаунт.`;
}

function renderFooterNote() {
  const note = $('footer-note');
  note.hidden = state.subscription.paid;
  note.textContent =
    'На бесплатном тарифе в конец каждого сообщения дописывается строка ' +
    'о том, каким ботом сделана рассылка.';
}

function countText() {
  const length = $('campaign-text').value.length;
  $('text-count').textContent = `${length} из ${state.limits.max_text}`;
}

async function rescan() {
  const button = $('rescan');
  const done = busy(button, 'Читаем чаты…');
  const note = $('scan-note');
  note.hidden = true;
  const result = await api('/api/chats/scan', { account_id: draft.accountId });
  done();
  if (!result.ok) {
    note.textContent = result.error;
    note.hidden = false;
    haptic('error');
    return;
  }
  note.textContent =
    `Нашлось ${result.chats} ${plural(result.chats, 'чат', 'чата', 'чатов')}` +
    ` и ${result.folders} ${plural(result.folders, 'папка', 'папки', 'папок')}.`;
  note.hidden = false;
  haptic('success');
  await loadChats();
}

async function createCampaign() {
  const button = $('campaign-start');
  fail('new-err', '');

  const text = $('campaign-text').value.trim();
  if (draft.content === 'text' && !text) {
    fail('new-err', 'Напишите текст сообщения.');
    return;
  }
  if (draft.content === 'saved' && !draft.savedId) {
    fail('new-err', 'Выберите сообщение из «Избранного».');
    return;
  }
  if (draft.source === 'chats' && !draft.chatIds.size) {
    fail('new-err', 'Выберите хотя бы один чат.');
    return;
  }
  if (draft.source === 'folder' && !draft.folderId) {
    fail('new-err', 'Выберите папку.');
    return;
  }

  const done = busy(button, 'Запускаем…');
  const result = await api('/api/campaign/create', {
    account_id: draft.accountId,
    text,
    interval: draft.interval,
    source: draft.source,
    folder_id: draft.folderId,
    chat_ids: [...draft.chatIds],
    content: draft.content,
    saved_id: draft.savedId,
  });
  done();
  if (!result.ok) {
    fail('new-err', result.error);
    return;
  }
  haptic('success');
  await refresh();
}

// --- диалоги ----------------------------------------------------------

/* Свои диалоги Telegram выглядят родными, но есть не во всех версиях
   клиента — старые молча ничего не покажут. Поэтому проверяем и падаем
   на браузерные: сообщение важнее вида. */

function alertBox(text) {
  if (tg && tg.showAlert) tg.showAlert(text);
  else window.alert(text);
}

function confirmBox(text, callback) {
  if (tg && tg.showConfirm) tg.showConfirm(text, callback);
  else callback(window.confirm(text));
}

// --- вход по номеру ---------------------------------------------------

function startFlow() {
  fail('phone-err', '');
  $('phone').value = '';
  show('phone');
  $('phone').focus();
}

async function requestCode() {
  const button = $('phone-next');
  fail('phone-err', '');
  const phone = $('phone').value.trim();
  if (phone.replace(/\D/g, '').length < 10) {
    fail('phone-err', 'Введите номер полностью, с кодом страны.');
    return;
  }
  const done = busy(button, 'Запрашиваем код…');
  const result = await api('/api/login/start', { phone });
  done();
  if (!result.ok) {
    fail('phone-err', result.error);
    return;
  }
  login = { token: result.token, phone: result.phone, stage: 'code' };
  haptic('light');
  openCode();
}

function openCode() {
  fail('code-err', '');
  $('code').value = '';
  $('code-phone').textContent = login.phone;
  show('code');
  $('code').focus();
}

/* Есть ли ещё живой вход. Проверяется перед каждой отправкой, потому
   что обнулиться он может под руками: после ответа с restart мы уводим
   человека к номеру не сразу, а через секунду — чтобы он успел прочесть
   причину, — и в это окно повторное нажатие «Войти» приходило на пустой
   login. */
function loginLost(errorBox) {
  if (login && login.token) return false;
  fail(errorBox, 'Вход уже не активен. Начните с номера заново.');
  setTimeout(startFlow, 1200);
  return true;
}

async function submitCode() {
  const button = $('code-next');
  fail('code-err', '');
  if (loginLost('code-err')) return;
  const code = $('code').value.replace(/\D/g, '');
  if (!code) {
    fail('code-err', 'Введите код из Telegram.');
    return;
  }
  const done = busy(button, 'Входим…');
  const result = await api('/api/login/code', { token: login.token, code });
  done();
  if (!result.ok) {
    fail('code-err', result.error);
    // restart — код или попытка уже мертвы: возвращаем к номеру, иначе
    // человек будет вводить код в форму, которая его больше не примет.
    if (result.restart) {
      login = null;
      setTimeout(startFlow, 1200);
    }
    return;
  }
  if (result.stage === 'password') {
    login.stage = 'password';
    openPassword();
    return;
  }
  finish(result.account);
}

function openPassword() {
  fail('password-err', '');
  $('password').value = '';
  show('password');
  $('password').focus();
}

async function submitPassword() {
  const button = $('password-next');
  fail('password-err', '');
  if (loginLost('password-err')) return;
  const password = $('password').value;
  if (!password) {
    fail('password-err', 'Введите облачный пароль.');
    return;
  }
  const done = busy(button, 'Входим…');
  const result = await api('/api/login/password', { token: login.token, password });
  done();
  // Пароль не остаётся в поле: приложение сворачивают, а не закрывают, и
  // открытый пароль лежал бы в форме до перезагрузки страницы.
  $('password').value = '';
  if (!result.ok) {
    fail('password-err', result.error);
    if (result.restart) {
      login = null;
      setTimeout(startFlow, 1200);
    }
    return;
  }
  finish(result.account);
}

function finish(account) {
  login = null;
  haptic('success');
  $('done-who').textContent = account.username
    ? `${account.name} · @${account.username}`
    : `${account.name} · ${account.phone}`;
  show('done');
}

async function cancelFlow() {
  if (login && login.token) {
    await api('/api/login/cancel', { token: login.token });
  }
  login = null;
  await refresh();
}

// --- запуск -----------------------------------------------------------

function wire() {
  $('add-account').onclick = startFlow;
  $('phone-next').onclick = requestCode;
  $('code-next').onclick = submitCode;
  $('password-next').onclick = submitPassword;
  $('done-back').onclick = refresh;

  for (const button of document.querySelectorAll('[data-back]')) {
    button.onclick = cancelFlow;
  }
  for (const button of document.querySelectorAll('[data-home]')) {
    button.onclick = refresh;
  }

  $('add-tdata').onclick = openTdata;
  $('tdata-send').onclick = uploadTdata;
  $('add-campaign').onclick = openNew;
  $('rescan').onclick = rescan;
  $('campaign-start').onclick = createCampaign;
  $('campaign-text').addEventListener('input', countText);
  $('chat-search').addEventListener('input', renderChatList);
  $('campaign-account').addEventListener('change', () => {
    draft.accountId = Number($('campaign-account').value);
    // Чаты у каждого аккаунта свои: выбранное от прошлого аккаунта
    // здесь не просто лишнее, оно относится к чужому списку.
    draft.chatIds.clear();
    draft.folderId = null;
    draft.savedId = null;
    loadChats();
  });
  for (const tab of document.querySelectorAll('.tab')) {
    tab.onclick = () => setTab(tab.dataset.tab);
  }
  for (const tab of document.querySelectorAll('.ctab')) {
    tab.onclick = () => setContentTab(tab.dataset.ctab);
  }
  for (const tab of document.querySelectorAll('.etab')) {
    tab.onclick = () => setEditTab(tab.dataset.etab);
  }
  for (const item of document.querySelectorAll('.tab-item')) {
    item.onclick = () => setPane(item.dataset.pane);
  }
  $('edit-save').onclick = saveEdit;
  $('edit-text').addEventListener('input', () => {
    $('edit-count').textContent =
      `${$('edit-text').value.length} из ${state.limits.max_text}`;
  });

  // Enter на телефонной клавиатуре — самый естественный способ
  // отправить короткое поле, и без этого человек ищет кнопку глазами.
  $('phone').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') requestCode();
  });
  $('code').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitCode();
  });
  $('password').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitPassword();
  });
  // Код обычно вставляют целиком: как только цифр набралось пять,
  // отправляем сами — лишнее нажатие тут только мешает.
  $('code').addEventListener('input', () => {
    if ($('code').value.replace(/\D/g, '').length === 5) submitCode();
  });

  if (tg && tg.BackButton) tg.BackButton.onClick(goBack);
}

async function boot() {
  if (!tg || !tg.initData) {
    // Ни SDK, ни подписи — значит, страницу открыли не из Telegram.
    // Спрашивать сервер бессмысленно: он ответит отказом по подписи.
    show('outside');
    return;
  }
  tg.ready();
  tg.expand();
  wire();
  await refresh();
  // Вход, начатый до сворачивания приложения, продолжается с того же
  // шага — за кодом человек уходит в другой чат и возвращается.
  if (login && login.stage === 'code') openCode();
  else if (login && login.stage === 'password') openPassword();
}

/** Наружу — для отладки: перезапустить приложение из консоли. */
window.rassylka = { boot, refresh, get state() { return state; } };

boot();
