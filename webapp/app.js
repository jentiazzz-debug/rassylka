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

/** Иконка из набора в index.html. Своя разметка, а не эмодзи: эмодзи
 *  рисуются шрифтом системы и на разных устройствах выглядят по-разному,
 *  а половина из них ещё и цветная — в строгом оформлении это мусор. */
function icon(name, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ic' + (cls ? ' ' + cls : ''));
  // viewBox обязателен. Без него svg показывает не всю иконку, а её
  // левый верхний угол размером в свою ширину: рисунки нарисованы в
  // сетке 24×24, а на экране занимают 15–20 пикселей.
  svg.setAttribute('viewBox', '0 0 24 24');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}

const SCREENS = [
  'loading', 'main', 'phone', 'code', 'password', 'done', 'tdata', 'new',
  'edit', 'log', 'topup', 'watch', 'outside',
];

/** Разделы главного экрана. */
const PANES = ['profile', 'mail', 'accounts', 'admin'];
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
  if (name === 'admin') renderAdmin();
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

/** Кнопка на время запроса: заблокировать и сказать, что идёт работа.
 *
 *  Сохраняем разметку, а не текст: внутри кнопок лежат иконки, и
 *  восстановление через textContent их бы стёрло. Разметка тут своя, из
 *  кода — не пользовательская. */
function busy(button, text) {
  if (button.dataset.saved === undefined) button.dataset.saved = button.innerHTML;
  button.disabled = true;
  button.textContent = text;
  return () => {
    button.disabled = false;
    button.innerHTML = button.dataset.saved;
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
    $('sub-title').textContent = 'Подписка активна';
    $('sub-note').textContent =
      `До ${dateText(sub.until)}. Подпись о боте в сообщениях не добавляется.`;
  } else if (sub.kind === 'trial') {
    $('sub-title').textContent = 'Пробный период';
    $('sub-note').textContent =
      `Осталось ${days} ${plural(days, 'день', 'дня', 'дней')}, до ` +
      `${dateText(sub.until)}. Пока он идёт, в конце каждого сообщения ` +
      'рассылки дописывается строка о том, каким ботом она сделана.';
  } else {
    $('sub-title').textContent = 'Бесплатный период закончился';
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
  badge.append(icon(alive ? 'check' : 'x'), alive ? 'подключён' : 'не работает');
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
  running: { label: 'идёт', css: '', icon: 'play' },
  paused: { label: 'на паузе', css: 'paused', icon: 'pause' },
  stopped: { label: 'остановлена', css: 'stopped', icon: 'x' },
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
  badge.append(icon(status.icon), status.label);
  top.appendChild(badge);
  row.appendChild(top);

  const text = document.createElement('div');
  text.className = 'campaign-text';
  // У рассылки материалом текст в карточке обманчив: уйдёт не он, а
  // сообщение из «Избранного» — с медиа и оформлением.
  text.textContent = campaign.content === 'saved'
    ? 'Сообщение из «Избранного» — с медиа и оформлением'
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
    note.textContent = campaign.note;
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

function renderPaidLimit() {
  const field = $('paid-limit');
  if (document.activeElement !== field) {
    field.value = state.paid_max_stars || 0;
  }
  field.max = state.limits.paid_cap || 100;
}

async function savePaidLimit(button) {
  const stars = parseInt($('paid-limit').value, 10) || 0;
  const done = busy(button, '…');
  const result = await api('/api/paid-limit', { stars });
  done();
  if (!result.ok) return alertBox(result.error);
  state.paid_max_stars = result.paid_max_stars;
  renderPaidLimit();
  haptic('success');
}

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

  const name = $('me-name');
  name.textContent = me.name;
  if (me.is_premium) name.appendChild(icon('star'));
  $('me-nick').textContent = me.username ? '@' + me.username : 'без ника';
  $('me-id').textContent = 'ID: ' + me.id;

  $('coins-value').textContent = me.stats.coins || 0;
  $('coins-label').textContent = me.coin_name + ' — ими оплачивается подписка';

  fillStats($('me-stats'), [
    ['Аккаунтов', me.stats.accounts || 0],
    ['Рассылок', me.stats.campaigns || 0],
    ['Сообщений отправлено', me.stats.sent || 0],
    ['Звёзд потрачено', me.stats.stars || 0],
  ]);

  fillStats($('coin-history'), me.history.length
    ? me.history.map((op) => [
      op.reason,
      (op.delta > 0 ? '+' : '') + op.delta,
    ])
    : [['Пока пусто', '—']]);

  renderReferral();

  const support = $('me-support');
  support.hidden = !state.support_url;
  if (state.support_url) support.href = state.support_url;

  renderBanner();
  renderPlans();
  renderLegal();
  $('tab-admin').hidden = !state.is_admin;
}

/** Пачки за рубли. Блока нет вовсе, пока оплата рублями не настроена:
 *  пустой раздел «оплата недоступна» только путает. */


/** Баннер владельца из /admin. Нет заголовка — нет и блока: пустая
 *  панель наверху профиля выглядит как недогрузившийся экран. */
function renderBanner() {
  const banner = state.banner || {};
  const title = (banner.title || '').trim();
  $('app-banner').hidden = !title;
  if (!title) return;
  $('banner-title').textContent = title;
  $('banner-text').textContent = (banner.text || '').trim();
  $('banner-text').hidden = !(banner.text || '').trim();
}

function renderLegal() {
  const box = $('legal-links');
  box.textContent = '';
  const links = me.legal || {};
  const rows = [
    ['Пользовательское соглашение', links.terms],
    ['Политика конфиденциальности', links.privacy],
    ['Тарифы', links.tariffs],
    ['Поддержка и реквизиты', links.support],
  ];
  for (const [title, href] of rows) {
    if (!href) continue;
    const row = document.createElement('button');
    row.className = 'row';
    const body = document.createElement('span');
    body.className = 'row-body';
    const name = document.createElement('span');
    name.className = 'row-title';
    name.textContent = title;
    body.appendChild(name);
    row.append(body, icon('chevron'));
    row.onclick = () => {
      if (tg && tg.openLink) tg.openLink(href);
      else window.open(href, '_blank');
    };
    box.appendChild(row);
  }
}

/** Строки «название — значение». clear=false дописывает к тому, что уже
 *  есть в блоке: в карточке приглашений над ними идёт пояснение. */
function fillStats(box, rows, clear = true) {
  if (clear) box.textContent = '';
  for (const [label, value] of rows) {
    const row = document.createElement('div');
    row.className = 'row stat';
    const left = document.createElement('span');
    left.textContent = label;
    const right = document.createElement('span');
    right.textContent = value;
    row.append(left, right);
    box.appendChild(row);
  }
}

function renderReferral() {
  const box = $('referral');
  const ref = me.referral;
  box.textContent = '';

  const about = document.createElement('div');
  about.className = 'row';
  const aboutText = document.createElement('span');
  aboutText.className = 'hint';
  aboutText.textContent =
    `За каждого, кто придёт по вашей ссылке — ${ref.coins} ${me.coin_name}. ` +
    `Дальше ${ref.percent}% с каждого его пополнения.`;
  about.appendChild(aboutText);
  box.appendChild(about);

  fillStats(box, [
    ['Пришло по ссылке', ref.invited],
    ['Заработано', ref.earned + ' ' + me.coin_name],
  ], false);

  if (!ref.link) return;

  const link = document.createElement('div');
  link.className = 'invite-link';
  link.append(icon('link', 'ic-s'), ref.link);
  box.appendChild(link);

  const share = document.createElement('button');
  share.className = 'row row-sep';
  share.append(icon('users', 'lead'));
  const shareBody = document.createElement('span');
  shareBody.className = 'row-body';
  const shareTitle = document.createElement('span');
  shareTitle.className = 'row-title';
  shareTitle.textContent = 'Позвать друзей';
  shareBody.appendChild(shareTitle);
  share.append(shareBody, icon('chevron'));
  share.onclick = () => {
    const text = 'Рассылки в Telegram со своего аккаунта — попробуй';
    const url = 'https://t.me/share/url?url=' + encodeURIComponent(ref.link)
      + '&text=' + encodeURIComponent(text);
    // openTelegramLink закрывает мини-апп и открывает окно «поделиться»
    // прямо в клиенте. Без него ссылка ушла бы во внешний браузер.
    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, '_blank');
  };
  box.appendChild(share);
}

function renderPlans() {
  const box = $('plans');
  box.textContent = '';
  const balance = me.stats.coins || 0;

  for (const plan of me.plans) {
    const button = document.createElement('button');
    button.className = 'row';
    button.appendChild(icon('clock', 'lead'));

    const body = document.createElement('div');
    body.className = 'row-body';
    const name = document.createElement('div');
    name.className = 'row-title';
    name.textContent = plan.days >= 30
      ? 'Месяц'
      : `${plan.days} ${plural(plan.days, 'день', 'дня', 'дней')}`;
    const day = document.createElement('div');
    day.className = 'row-note';
    // Не хватает монет — говорим об этом на самой кнопке, а не после
    // нажатия: отказ по нажатию читается как поломка.
    day.textContent = balance < plan.coins
      ? `не хватает ${plan.coins - balance} ${me.coin_name}`
      : (plan.days > 1
        ? `${Math.round(plan.coins / plan.days)} ${me.coin_name} в день`
        : '');
    body.append(name, day);
    button.appendChild(body);

    const price = document.createElement('span');
    price.className = 'row-value';
    price.append(String(plan.coins), icon('coin', 'ic-s'));
    button.appendChild(price);

    button.onclick = () => subscribe(plan, button);
    box.appendChild(button);
  }
}

// --- пополнение --------------------------------------------------------

/** Что выбрано на экране пополнения: способ и количество монет. */
let topup = { method: null, coins: null };

/** Способы оплаты. Порядок неслучаен: звёзды не уводят из Telegram, и
 *  для большинства это самый короткий путь. */
function methods() {
  const limits = me.min_coins || {};
  const out = [{
    id: 'stars', icon: 'star', title: 'Telegram Stars',
    note: 'внутри Telegram, без сторонних сайтов',
    packs: me.packs.map((p) => ({
      coins: p.coins, price: p.stars, base: p.base,
      popular: p.popular, label: p.stars + ' \u2605',
    })),
    min: limits.stars || me.custom.min,
    price: (coins) => coins * me.custom.rate + ' \u2605',
  }];
  if ((me.rub_packs || []).length) {
    out.push({
      id: 'rub', icon: 'ruble', title: 'Карта или СБП',
      note: 'оплата на странице платёжной организации',
      packs: me.rub_packs.map((p) => ({
        coins: p.coins, price: p.rub, label: Math.round(p.rub) + ' \u20bd',
      })),
      min: limits.rub || me.custom.min,
      price: (coins) => Math.round(coins * me.rub_per_coin) + ' \u20bd',
    });
  }
  if ((me.crypto_packs || []).length) {
    out.push({
      id: 'crypto', icon: 'cryptobot', title: 'CryptoBot',
      note: 'криптовалютой \u00b7 $1 = ' + me.coins_per_usd + ' ' + me.coin_name,
      packs: me.crypto_packs.map((p) => ({
        coins: p.coins, price: p.usd, label: '$' + p.usd,
      })),
      min: limits.crypto || me.custom.min,
      price: (coins) => '$' + (coins / me.coins_per_usd).toFixed(2),
    });
  }
  // Второй криптокошелёк рядом с первым, а не вместо него: кошелёк у
  // человека уже какой-то один, и «заведите нужный» — верный способ не
  // получить оплату вовсе. Цена и там и там одна.
  if ((me.xrocket_packs || []).length) {
    out.push({
      id: 'xrocket', icon: 'xrocket', title: 'xRocket',
      note: 'криптовалютой \u00b7 $1 = ' + me.coins_per_usd + ' ' + me.coin_name,
      packs: me.xrocket_packs.map((p) => ({
        coins: p.coins, price: p.usd, label: '$' + p.usd,
      })),
      min: limits.xrocket || me.custom.min,
      price: (coins) => '$' + (coins / me.coins_per_usd).toFixed(2),
    });
  }
  return out;
}

function openTopup() {
  topup = { method: null, coins: null };
  renderMethods();
  $('topup-body').hidden = true;
  show('topup');
}

function renderMethods() {
  const box = $('pay-methods');
  box.textContent = '';
  for (const method of methods()) {
    const row = document.createElement('button');
    row.className = 'row';
    row.appendChild(icon(method.icon, 'lead'));

    const body = document.createElement('div');
    body.className = 'row-body';
    const title = document.createElement('div');
    title.className = 'row-title';
    title.textContent = method.title;
    const note = document.createElement('div');
    note.className = 'row-note';
    note.textContent = method.note;
    body.append(title, note);
    row.appendChild(body);
    row.appendChild(icon('chevron'));

    row.onclick = () => {
      topup = { method: method.id, coins: null };
      haptic('light');
      renderMethods();
      renderTopupPacks();
    };
    box.appendChild(row);
  }
}

function currentMethod() {
  return methods().find((m) => m.id === topup.method) || null;
}

function renderTopupPacks() {
  const method = currentMethod();
  $('topup-body').hidden = !method;
  if (!method) return;

  if (topup.coins === null) {
    const popular = method.packs.find((p) => p.popular) || method.packs[0];
    topup.coins = popular ? popular.coins : null;
  }

  $('topup-title').textContent = method.title;
  const best = method.packs.reduce((max, p) => Math.max(max, p.base > p.price
    ? Math.round((1 - p.price / p.base) * 100) : 0), 0);
  $('topup-aside').textContent = best ? 'скидка до ' + best + '%' : '';
  $('topup-custom').hidden = false;
  $('topup-own').hidden = true;
  $('topup-note').textContent = method.note;

  const box = $('topup-packs');
  box.textContent = '';
  for (const pack of method.packs) {
    const button = document.createElement('button');
    button.className = 'pack' + (pack.coins === topup.coins ? ' picked' : '');

    if (pack.popular) {
      const tag = document.createElement('span');
      tag.className = 'tag hot';
      tag.textContent = 'хит';
      button.appendChild(tag);
    }
    const off = pack.base > pack.price
      ? Math.round((1 - pack.price / pack.base) * 100) : 0;
    if (off) {
      const save = document.createElement('span');
      save.className = 'tag';
      save.textContent = '\u2212' + off + '%';
      button.appendChild(save);
    }

    const count = document.createElement('div');
    count.className = 'pack-count';
    count.textContent = pack.coins;
    const label = document.createElement('div');
    label.className = 'pack-label';
    label.textContent = me.coin_name;
    const price = document.createElement('div');
    price.className = 'pack-price';
    if (off) {
      const was = document.createElement('span');
      was.className = 'pack-was';
      was.textContent = pack.base;
      price.appendChild(was);
    }
    price.append(pack.label);

    button.append(count, label, price);
    button.onclick = () => {
      topup.coins = pack.coins;
      renderTopupPacks();
      haptic('light');
    };
    box.appendChild(button);
  }

  // Готовая пачка идёт по своей цене: в ней заложена скидка, и
  // пересчёт по базовому курсу отменил бы её тому, кто ввёл то же число
  // руками. Всё остальное — по базовому курсу способа.
  const chosen = method.packs.find((p) => p.coins === topup.coins);
  $('topup-total').textContent = chosen ? chosen.label
    : (topup.coins ? method.price(topup.coins) : '\u2014');
  $('topup-pay').disabled = !topup.coins;
}

/** Показать поле для своего количества и подсказать пределы. */
function askTopupCustom() {
  const method = currentMethod();
  if (!method) return;
  const field = $('topup-own-input');
  // Нижний порог берётся у способа, а не общий: у эквайринга своя
  // минимальная сумма, у криптокошелька своя, и общее число обмануло бы
  // человека — он ввёл бы допустимое, а счёт не открылся бы.
  field.placeholder = 'От ' + method.min + ' до ' + me.custom.max;
  field.min = method.min;
  field.max = me.custom.max;
  field.value = topup.coins || '';
  $('topup-custom').hidden = true;
  $('topup-own').hidden = false;
  field.focus();
}

function applyTopupCustom() {
  const method = currentMethod();
  if (!method) return;
  const min = method.min;
  const max = me.custom.max;
  const coins = parseInt(
    String($('topup-own-input').value).replace(/[^0-9]/g, ''), 10);
  if (!coins || coins < min || coins > max) {
    alertBox('Этим способом можно от ' + min + ' до ' + max + ' '
      + me.coin_name + '.');
    return;
  }
  topup.coins = coins;
  const own = $('topup-own');
  renderTopupPacks();
  // renderTopupPacks прячет поле, но человек только что им пользовался:
  // пусть остаётся открытым, чтобы было видно введённое число.
  $('topup-custom').hidden = true;
  own.hidden = false;
  $('topup-own-input').value = coins;
  haptic('light');
}

async function payTopup() {
  const method = currentMethod();
  if (!method || !topup.coins) return;
  const button = $('topup-pay');

  if (method.id === 'stars') {
    const done = busy(button, 'Открываем счёт…');
    const result = await api('/api/invoice', { coins: topup.coins });
    done();
    if (!result.ok) return alertBox(result.error);
    if (!tg || !tg.openInvoice) {
      return alertBox('Ваша версия Telegram не умеет открывать счёт.');
    }
    // Ответ openInvoice — статус в браузере, и монеты по нему никто не
    // начисляет: подтверждение приходит боту отдельным апдейтом.
    tg.openInvoice(result.link, (status) => {
      if (status === 'paid') {
        haptic('success');
        setTimeout(refresh, 1400);
      } else if (status === 'failed') {
        alertBox('Оплата не прошла.');
      }
    });
    return;
  }

  const path = '/api/invoice/' + method.id;
  const done = busy(button, 'Открываем счёт…');
  const result = await api(path, { coins: topup.coins });
  done();
  if (!result.ok) return alertBox(result.error);
  // Внешняя платёжная страница: внутри мини-аппа не сработают ни
  // приложение банка, ни возврат по СБП, ни кошелёк.
  if (tg && tg.openLink) tg.openLink(result.url);
  else window.open(result.url, '_blank');
  setTimeout(refresh, 30000);
}

async function subscribe(plan, button) {
  const done = busy(button, 'Оформляем…');
  const result = await api('/api/subscribe', { days: plan.days });
  done();
  if (!result.ok) {
    alertBox(result.error);
    return;
  }
  haptic('success');
  await refresh();
}


function renderMain() {
  renderSubscription();
  renderAccounts();
  renderCampaigns();
  renderWatches();
  renderPaidLimit();
  renderProfile();
  setPane(pane);
  show('main');
}

// --- правка рассылки ---------------------------------------------------

/** Что правим. Чаты сюда не входят: круг идёт, и менять список на ходу
 *  значит написать в одни чаты дважды, а другие пропустить. */
let editing = null;

/** Разложить сохранённые варианты обратно на то, чем их правят.

    Вариант с медиа И текстом — это текст с прикреплением, а не «просто
    материал»: свалив его в материалы, правка потеряла бы текст. */
function splitVariants(variants) {
  const texts = [];
  const attach = [];
  const savedIds = new Set();
  for (const item of variants || []) {
    const text = (item.text || '').trim();
    if (item.content === 'saved' && !text) {
      savedIds.add(item.saved_id);
      continue;
    }
    texts.push(item.text || '');
    attach.push(item.content === 'saved' ? item.saved_id : null);
  }
  return { texts, attach, savedIds };
}

async function openEdit(campaign) {
  const parts = splitVariants(campaign.variants);
  editing = {
    id: campaign.id,
    accountId: campaign.account_id,
    texts: parts.texts,
    attach: parts.attach,
    savedIds: parts.savedIds,
    interval: campaign.interval,
    pick: campaign.pick || 'random',
  };
  if (!editing.texts.length && !editing.savedIds.size) {
    editing.texts = [''];
    editing.attach = [null];
  }

  fail('edit-err', '');
  $('edit-about').textContent =
    `${campaign.chats} ${plural(campaign.chats, 'чат', 'чата', 'чатов')} · ` +
    'список чатов не меняется — круг уже идёт';
  $('edit-title').value = campaign.title;
  renderEditVariants();
  setEditPick(editing.pick);
  renderEditIntervals();
  show('edit');

  hintInto($('edit-materials'), 'Загружаем…');
  await loadMaterials(editing.accountId);
  renderMaterials($('edit-materials'), editing.savedIds, renderEditCount, true);
}

function renderEditVariants() {
  const box = $('edit-variants');
  box.textContent = '';
  editing.texts.forEach((value, index) => {
    const row = document.createElement('div');
    row.className = 'variant';

    const area = document.createElement('textarea');
    area.value = value;
    area.placeholder = 'Текст, который уйдёт в чат';
    area.oninput = () => {
      editing.texts[index] = area.value;
      renderEditCount();
    };
    row.appendChild(area);

    if (editing.texts.length > 1) {
      const drop = document.createElement('button');
      drop.className = 'variant-drop';
      drop.appendChild(icon('x', 'ic-s'));
      drop.onclick = () => {
        editing.texts.splice(index, 1);
        editing.attach.splice(index, 1);
        renderEditVariants();
      };
      row.appendChild(drop);
    }
    row.appendChild(attachRow(editing, index, renderEditVariants));
    box.appendChild(row);
  });
  renderEditCount();
  $('edit-variant-add').disabled =
    editing.texts.length >= state.limits.max_variants;
}

function editVariants() {
  return packVariants(editing);
}

function renderEditCount() {
  const count = editVariants().length;
  $('edit-variant-count').textContent = count
    ? `${count} ${plural(count, 'вариант', 'варианта', 'вариантов')}`
    : '';
}

function setEditPick(mode) {
  editing.pick = mode;
  for (const tab of document.querySelectorAll('.etab')) {
    tab.classList.toggle('active', tab.dataset.pick === mode);
  }
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
  const variants = editVariants();
  if (!variants.length) {
    fail('edit-err', 'Добавьте текст или отметьте сообщение из «Избранного».');
    return;
  }
  const done = busy(button, 'Сохраняем…');
  const result = await api('/api/campaign/edit', {
    id: editing.id,
    title: $('edit-title').value.trim(),
    variants,
    interval: editing.interval,
    pick: editing.pick,
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

    row.appendChild(icon(send.ok ? 'check' : 'x',
      'ic-s log-mark ' + (send.ok ? 'good' : 'bad')));

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
  text: 'text', photo: 'image', video: 'video', gif: 'film',
  sticker: 'smile', audio: 'mic', document: 'file',
};

let draft = null;
let picker = { chats: [], folders: [], materials: [] };

/** Список материалов. `multi` — набор отмеченных (создание рассылки),
 *  иначе одиночный выбор с колбэком (правка). */
function renderMaterials(box, selected, onPick, multi) {
  box.textContent = '';
  if (!picker.materials.length) {
    hintInto(box, 'В «Избранном» этого аккаунта пока пусто. Отправьте туда '
      + 'сообщение — с того самого аккаунта, с которого рассылаете, — '
      + 'и нажмите «Перечитать «Избранное»» ниже.');
    return;
  }
  for (const material of picker.materials) {
    const row = document.createElement('label');
    row.className = 'pick';

    const input = document.createElement('input');
    input.type = multi ? 'checkbox' : 'radio';
    input.name = box.id;
    input.checked = multi
      ? selected.has(material.msg_id)
      : selected === material.msg_id;
    input.onchange = () => {
      if (multi) {
        input.checked ? selected.add(material.msg_id)
                      : selected.delete(material.msg_id);
        if (onPick) onPick();
      } else if (onPick) {
        onPick(material.msg_id);
      }
    };
    row.appendChild(input);
    row.appendChild(icon(MATERIAL_ICONS[material.kind] || 'text', 'pick-mark'));

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

const STEPS = ['where', 'what', 'how'];
const STEP_LAST = STEPS.length - 1;

function openNew() {
  const alive = state.accounts.filter((a) => a.status === 'ok');
  if (!alive.length) return;

  draft = {
    accountId: alive[0].id,
    chatIds: new Set(),
    savedIds: new Set(),
    texts: [''],
    // Что прикреплено к каждому тексту — список той же длины, что и
    // texts: так правка и удаление вариантов остаются простыми
    // операциями над двумя массивами, а не над списком объектов.
    attach: [null],
    interval: 900,
    pick: 'random',
    step: 0,
  };
  $('chat-search').value = '';
  fail('new-err', '');
  $('scan-note').hidden = true;

  // Аккаунт спрашиваем, только когда их несколько: выбор из одного
  // пункта — лишний вопрос на первом же шаге.
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
  renderVariants();
  setPick('random');
  setTab('chats');
  goStep(0);
  show('new');
  loadChats();
}

function goStep(index) {
  draft.step = Math.max(0, Math.min(STEP_LAST, index));
  fail('new-err', '');
  for (let i = 0; i < STEPS.length; i++) {
    $('step-' + STEPS[i]).hidden = i !== draft.step;
  }
  for (const tab of document.querySelectorAll('.step')) {
    const at = Number(tab.dataset.step);
    tab.classList.toggle('active', at === draft.step);
    tab.classList.toggle('done', at < draft.step);
  }
  $('step-back').textContent = draft.step === 0 ? 'Отмена' : 'Назад';
  $('step-next').textContent = draft.step === STEP_LAST ? 'Запустить' : 'Далее';
  if (draft.step === STEP_LAST) renderSummary();
  window.scrollTo(0, 0);
}

/** Что мешает уйти с текущего шага. Пустая строка — ничего.
 *  Проверяем по шагам, а не всё сразу в конце: иначе человек узнаёт о
 *  незаполненном первом шаге, уже дойдя до третьего. */
function stepProblem(step) {
  if (step === 0 && !draft.chatIds.size) return 'Выберите хотя бы один чат.';
  if (step === 1 && !collectVariants().length) {
    return 'Добавьте текст или отметьте сообщение из «Избранного».';
  }
  return '';
}

async function stepNext() {
  const problem = stepProblem(draft.step);
  if (problem) {
    fail('new-err', problem);
    return;
  }
  if (draft.step < STEP_LAST) {
    goStep(draft.step + 1);
    return;
  }
  await createCampaign();
}

function stepBack() {
  if (draft.step === 0) {
    refresh();
    return;
  }
  goStep(draft.step - 1);
}

function jumpStep(index) {
  // Вперёд — только через проверку, назад — свободно.
  if (index > draft.step) {
    for (let i = draft.step; i < index; i++) {
      const problem = stepProblem(i);
      if (problem) {
        goStep(i);
        fail('new-err', problem);
        return;
      }
    }
  }
  goStep(index);
}

// --- автокомментарии под постами ---------------------------------------

/** Что сейчас редактируется: новое наблюдение или существующее. */
let watching = null;

function renderWatches() {
  const box = $('watches');
  const list = state.watches || [];
  box.textContent = '';
  $('watches-empty').hidden = list.length > 0;

  for (const watch of list) {
    const card = document.createElement('div');
    card.className = 'campaign';

    const top = document.createElement('div');
    top.className = 'campaign-top';
    const name = document.createElement('span');
    name.className = 'campaign-name';
    name.textContent = watch.title;
    top.appendChild(name);
    top.appendChild(watchBadge(watch));
    card.appendChild(top);

    const what = document.createElement('div');
    what.className = 'campaign-text';
    const first = (watch.variants || [])[0] || {};
    what.textContent = (first.text || '').trim()
      || (first.saved_id ? 'сообщение из «Избранного»' : '—');
    card.appendChild(what);

    const facts = document.createElement('div');
    facts.className = 'campaign-facts';
    facts.textContent = [
      `ответов ${watch.sent_ok}`,
      watch.sent_err ? `ошибок ${watch.sent_err}` : '',
      `через ${watch.delay_min}\u2013${watch.delay_max} с`,
      watch.note,
    ].filter(Boolean).join(' \u00b7 ');
    card.appendChild(facts);

    const actions = document.createElement('div');
    actions.className = 'campaign-actions';
    actions.appendChild(smallButton(
      watch.status === 'running' ? 'Пауза' : 'Продолжить',
      () => toggleWatch(watch),
    ));
    actions.appendChild(smallButton('Изменить', () => openWatch(watch)));
    const drop = smallButton('Удалить', () => removeWatch(watch));
    drop.classList.add('danger');
    actions.appendChild(drop);
    card.appendChild(actions);

    box.appendChild(card);
  }
}

function watchBadge(watch) {
  const badge = document.createElement('span');
  badge.className = 'badge';
  if (watch.status === 'paused') badge.classList.add('paused');
  if (watch.status === 'stopped') badge.classList.add('stopped');
  badge.textContent = watch.status === 'running' ? 'следит'
    : (watch.status === 'paused' ? 'пауза' : 'остановлен');
  return badge;
}

function smallButton(label, onClick) {
  const button = document.createElement('button');
  button.className = 'btn small';
  button.textContent = label;
  button.onclick = () => onClick(button);
  return button;
}

/** Экран наблюдения. Без аргумента — новое, с наблюдением — правка. */
async function openWatch(watch) {
  const alive = (state.accounts || []).filter((a) => a.status === 'ok');
  if (!alive.length) {
    return alertBox('Сначала подключите аккаунт — комментировать некому.');
  }

  const parts = splitVariants(watch ? watch.variants : []);
  watching = {
    id: watch ? watch.id : 0,
    accountId: watch ? watch.account_id : alive[0].id,
    chatId: watch ? watch.chat_id : 0,
    texts: parts.texts.length ? parts.texts : [''],
    attach: parts.attach.length ? parts.attach : [null],
    savedIds: parts.savedIds,
    pick: watch ? watch.pick : 'random',
  };

  $('watch-head').textContent = watch ? 'Автокомментарий' : 'Новый автокомментарий';
  $('watch-save').textContent = watch ? 'Сохранить' : 'Включить';
  // Канал у наблюдения не меняется: сменить его — то же, что завести
  // другое наблюдение, а отметка последнего поста осталась бы от
  // прежнего канала и первый же ответ ушёл бы не туда.
  $('watch-channel-box').hidden = Boolean(watch);
  $('watch-title').hidden = !watch;
  if (watch) $('watch-title').textContent = watch.title;
  // По умолчанию ноль: ради раздач эту штуку и просили, а там
  // выигрывают секунды.
  $('watch-delay-min').value = watch ? watch.delay_min : 0;
  $('watch-delay-max').value = watch ? watch.delay_max : 0;
  setWatchPick(watching.pick);
  fail('watch-err', '');
  renderWatchVariants();
  show('watch');

  hintInto($('watch-channels'), 'Загружаем…');
  await loadMaterials(watching.accountId);
  renderWatchVariants();
  if (!watch) await loadWatchChannels();
}

/** Каналы аккаунта. Только каналы: комментарии бывают под их постами. */
async function loadWatchChannels() {
  const box = $('watch-channels');
  const result = await api('/api/chats', { account_id: watching.accountId });
  if (!result.ok) return hintInto(box, result.error);

  const channels = (result.chats || []).filter((c) => c.broadcast);
  box.textContent = '';
  if (!channels.length) {
    return hintInto(box, 'Каналов не нашлось. Подпишитесь на канал с этого '
      + 'аккаунта и обновите список чатов на шаге «Куда» новой рассылки.');
  }
  for (const channel of channels) {
    const row = document.createElement('label');
    row.className = 'pick';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'watch-channel';
    // В ответе /api/chats номер чата лежит в id, а не в chat_id: имена
    // полей наружу и внутри базы намеренно разные.
    input.checked = channel.id === watching.chatId;
    input.onchange = () => { watching.chatId = channel.id; };
    row.appendChild(input);
    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = channel.title;
    row.appendChild(title);
    box.appendChild(row);
  }
}

function renderWatchVariants() {
  const box = $('watch-variants');
  box.textContent = '';
  watching.texts.forEach((value, index) => {
    const row = document.createElement('div');
    row.className = 'variant';

    const area = document.createElement('textarea');
    area.value = value;
    area.placeholder = index === 0
      ? 'Что написать под постом'
      : 'Другая формулировка того же';
    area.oninput = () => { watching.texts[index] = area.value; };
    row.appendChild(area);

    if (watching.texts.length > 1) {
      const drop = document.createElement('button');
      drop.className = 'variant-drop';
      drop.appendChild(icon('x', 'ic-s'));
      drop.onclick = () => {
        watching.texts.splice(index, 1);
        watching.attach.splice(index, 1);
        renderWatchVariants();
      };
      row.appendChild(drop);
    }
    row.appendChild(attachRow(watching, index, renderWatchVariants));
    box.appendChild(row);
  });
  $('watch-variant-add').disabled =
    watching.texts.length >= state.limits.max_variants;
}

function setWatchPick(mode) {
  watching.pick = mode;
  for (const tab of document.querySelectorAll('.wtab')) {
    tab.classList.toggle('active', tab.dataset.pick === mode);
  }
}

async function saveWatch(button) {
  fail('watch-err', '');
  if (!watching.id && !watching.chatId) {
    return fail('watch-err', 'Выберите канал.');
  }
  const body = {
    account_id: watching.accountId,
    chat_id: watching.chatId,
    variants: packVariants(watching),
    delay_min: parseInt($('watch-delay-min').value, 10) || 0,
    delay_max: parseInt($('watch-delay-max').value, 10) || 0,
    pick: watching.pick,
  };
  if (watching.id) body.watch_id = watching.id;

  const done = busy(button, watching.id ? 'Сохраняем…' : 'Включаем…');
  const result = await api(
    watching.id ? '/api/watch/edit' : '/api/watch/create', body,
  );
  done();
  if (!result.ok) return fail('watch-err', result.error);
  state.watches = result.watches;
  renderWatches();
  haptic('success');
  await refresh();
}

async function toggleWatch(watch) {
  const result = await api('/api/watch/toggle', { watch_id: watch.id });
  if (!result.ok) return alertBox(result.error);
  state.watches = result.watches;
  renderWatches();
}

function removeWatch(watch) {
  confirmBox(`Удалить автокомментарий в «${watch.title}»?`, async (yes) => {
    if (!yes) return;
    const result = await api('/api/watch/delete', { watch_id: watch.id });
    if (!result.ok) return alertBox(result.error);
    state.watches = result.watches;
    renderWatches();
  });
}


// --- варианты сообщения ------------------------------------------------

/** Материалы, которым Telegram не даёт подписи. */
const NO_CAPTION = new Set(['sticker', 'round']);

function materialById(id) {
  return picker.materials.find((m) => m.msg_id === id) || null;
}

/** Собрать варианты: тексты (со своим медиа, если прикреплено) плюс
 *  материалы, отмеченные галочками отдельными вариантами. */
function packVariants(store) {
  const out = [];
  store.texts.forEach((raw, index) => {
    const text = (raw || '').trim();
    const saved_id = (store.attach || [])[index] || null;
    if (!text && !saved_id) return;
    if (saved_id) out.push({ content: 'saved', saved_id, text });
    else out.push({ content: 'text', text });
  });
  for (const saved_id of store.savedIds) {
    out.push({ content: 'saved', saved_id });
  }
  return out.slice(0, state.limits.max_variants);
}

function collectVariants() {
  return packVariants(draft);
}

/** Строка «что прикреплено» под текстом варианта.

    Список берётся из «Избранного» аккаунта, а не загрузкой файла:
    файл уже лежит у Telegram, и пересылать его через наш сервер значит
    хранить чужие фото и голосовые у себя — ради того же результата. */
function attachRow(store, index, rerender) {
  const row = document.createElement('div');
  row.className = 'variant-attach';
  const chosen = store.attach[index] ? materialById(store.attach[index]) : null;

  if (chosen) {
    row.appendChild(icon(MATERIAL_ICONS[chosen.kind] || 'file', 'ic-s'));
    const name = document.createElement('span');
    name.className = 'attach-name';
    name.textContent = chosen.preview || 'Сообщение';
    row.appendChild(name);

    const drop = document.createElement('button');
    drop.className = 'attach-drop';
    drop.title = 'Открепить';
    drop.appendChild(icon('x', 'ic-s'));
    drop.onclick = () => {
      store.attach[index] = null;
      rerender();
    };
    row.appendChild(drop);
    return row;
  }

  const add = document.createElement('button');
  add.className = 'attach-add';
  add.appendChild(icon('clip', 'ic-s'));
  add.append('Прикрепить');
  add.onclick = () => {
    if (!picker.materials.length) {
      // Пустое «Избранное» больше не тупик: тут же и загружаем.
      return pickFileFor(store, index, rerender);
    }
    row.textContent = '';
    row.appendChild(buildAttachPicker(store, index, rerender));
    const upload = document.createElement('button');
    upload.className = 'attach-drop';
    upload.title = 'Загрузить файл';
    upload.appendChild(icon('upload', 'ic-s'));
    upload.onclick = () => pickFileFor(store, index, rerender);
    row.appendChild(upload);
  };
  row.appendChild(add);

  const upload = document.createElement('button');
  upload.className = 'attach-add';
  upload.appendChild(icon('upload', 'ic-s'));
  upload.append('Загрузить');
  upload.onclick = () => pickFileFor(store, index, rerender);
  row.appendChild(upload);
  return row;
}

/** Выбрать файл на телефоне и положить его в «Избранное» аккаунта.

    Через «Избранное», а не в своё хранилище: файл всё равно должен
    оказаться у Telegram — оттуда его берёт рассылка, — а держать чужие
    фото и голосовые у себя ради того же результата незачем. */
function pickFileFor(store, index, rerender) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*,video/*,audio/*,.gif,.mp4,.ogg,.mp3,.m4a,.pdf';
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const limit = (state.limits.max_upload_mb || 32) * 1024 * 1024;
    if (file.size > limit) {
      return alertBox('Файл больше ' + state.limits.max_upload_mb + ' МБ.');
    }
    await uploadMaterial(store, index, rerender, file);
  };
  input.click();
}

async function uploadMaterial(store, index, rerender, file) {
  const box = document.createElement('div');
  box.className = 'hint small pad';
  box.textContent = 'Загружаем «' + file.name + '»…';
  const host = store === draft ? $('variant-list')
    : (store === editing ? $('edit-variants') : $('watch-variants'));
  host.appendChild(box);

  const form = new FormData();
  form.append('account_id', String(store.accountId));
  // Гифку и голосовое отправляем без сжатия: сжатая гифка перестаёт
  // быть гифкой, а голосовое — голосовым.
  const raw = /\.(gif|ogg|oga|mp3|m4a|pdf)$/i.test(file.name);
  form.append('as_file', raw ? '1' : '0');
  form.append('file', file, file.name);

  let result;
  try {
    const answer = await fetch('/api/material/upload', {
      method: 'POST',
      headers: { 'X-Init-Data': (tg && tg.initData) || '' },
      body: form,
    });
    result = await answer.json();
  } catch (error) {
    result = { ok: false, error: 'Загрузка не дошла. Попробуйте ещё раз.' };
  }
  box.remove();

  if (!result.ok) return alertBox(result.error);
  picker.materials = result.materials || [];
  store.attach[index] = result.msg_id;
  rerender();
  haptic('success');
}

/** Выбор материала — родным списком: в WebView он открывается
 *  привычным колесом, и своё меню тут только мешало бы. */
function buildAttachPicker(store, index, rerender) {
  const select = document.createElement('select');
  select.className = 'attach-pick';

  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = 'Выберите из «Избранного»…';
  select.appendChild(empty);

  for (const material of picker.materials) {
    const option = document.createElement('option');
    option.value = String(material.msg_id);
    option.textContent = (material.preview || 'Сообщение')
      + (NO_CAPTION.has(material.kind) ? ' — без подписи' : '');
    select.appendChild(option);
  }

  select.onchange = () => {
    const id = parseInt(select.value, 10);
    if (!id) return;
    const material = materialById(id);
    // К стикеру и кружку подпись не пристаёт — не по нашему решению, а
    // по устройству формата. Сказать об этом надо здесь, а не отказом
    // при запуске: там уже поздно.
    if (material && NO_CAPTION.has(material.kind)
        && (store.texts[index] || '').trim()) {
      alertBox('К стикеру и кружку Telegram не принимает подпись. '
        + 'Очистите текст этого варианта или выберите другое.');
      return;
    }
    store.attach[index] = id;
    rerender();
    haptic('light');
  };

  select.focus();
  return select;
}

function renderVariants() {
  const box = $('variant-list');
  box.textContent = '';

  draft.texts.forEach((value, index) => {
    const row = document.createElement('div');
    row.className = 'variant';

    const area = document.createElement('textarea');
    area.value = value;
    area.placeholder = index === 0
      ? 'Текст, который уйдёт в чат'
      : 'Другая формулировка того же';
    area.oninput = () => {
      draft.texts[index] = area.value;
      renderVariantCount();
    };
    row.appendChild(area);

    // Крестик только когда полей больше одного: у единственного поля он
    // предлагал бы остаться совсем без сообщения.
    if (draft.texts.length > 1) {
      const drop = document.createElement('button');
      drop.className = 'variant-drop';
      drop.appendChild(icon('x', 'ic-s'));
      drop.onclick = () => {
        draft.texts.splice(index, 1);
        draft.attach.splice(index, 1);
        renderVariants();
      };
      row.appendChild(drop);
    }
    row.appendChild(attachRow(draft, index, renderVariants));
    box.appendChild(row);
  });

  renderVariantCount();
  $('variant-add').disabled = draft.texts.length >= state.limits.max_variants;
}

function renderVariantCount() {
  const count = collectVariants().length;
  $('variant-count').textContent = count
    ? `${count} ${plural(count, 'вариант', 'варианта', 'вариантов')}`
    : '';
}

function addVariant() {
  if (draft.texts.length >= state.limits.max_variants) return;
  draft.texts.push('');
  draft.attach.push(null);
  renderVariants();
  haptic('light');
}

function setPick(mode) {
  draft.pick = mode;
  for (const tab of document.querySelectorAll('.ptab')) {
    tab.classList.toggle('active', tab.dataset.pick === mode);
  }
  // Сводка на этом же шаге — она обязана показывать то, что выбрано, а
  // не то, что было выбрано при входе на шаг.
  if (draft.step === STEP_LAST) renderSummary();
}

function renderSummary() {
  const variants = collectVariants();
  const chats = draft.chatIds.size;
  fillStats($('new-summary'), [
    ['Чатов', chats],
    ['Вариантов сообщения', variants.length],
    ['Интервал', human(draft.interval)],
    ['Круг займёт', human(chats * draft.interval)],
    ['Порядок', draft.pick === 'order' ? 'по очереди' : 'вразнобой'],
  ]);
}

function setTab(name) {
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
  renderMaterials($('material-list'), draft.savedIds, renderVariantCount, true);
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
  $('chat-picked').textContent = `Выбрано: ${count}`
    + (count ? ` ${plural(count, 'чат', 'чата', 'чатов')}` : '');
}

function renderFolders() {
  const box = $('folder-list');
  box.textContent = '';
  if (!picker.folders.length) {
    hintInto(box, 'Папок нет. Их создают в Telegram: Настройки → Папки с чатами.');
    return;
  }

  // Папка не выбирается «как папка» — по нажатию она отмечает все свои
  // чаты в списке. Так видно, что именно уйдёт в рассылку, и лишнее
  // можно снять галочкой: папка в Telegram нередко собрана не под
  // рассылку, и слать во всё подряд человек обычно не хочет.
  const known = new Set(picker.chats.map((chat) => chat.id));
  for (const folder of picker.folders) {
    const inList = folder.chat_ids.filter((id) => known.has(id));
    const picked = inList.length
      && inList.every((id) => draft.chatIds.has(id));

    const row = document.createElement('button');
    row.className = 'pick';
    row.appendChild(icon('folder', 'pick-mark'));

    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = folder.title;
    row.appendChild(title);

    const count = document.createElement('span');
    count.className = 'pick-kind';
    count.textContent = picked
      ? 'выбрана'
      : `${inList.length} ${plural(inList.length, 'чат', 'чата', 'чатов')}`;
    row.appendChild(count);

    row.onclick = () => {
      if (!inList.length) {
        alertBox('В этой папке нет чатов из списка. Обновите список чатов.');
        return;
      }
      // Повторное нажатие снимает выбор — иначе папку, нажатую по
      // ошибке, пришлось бы разбирать галочка за галочкой.
      if (picked) inList.forEach((id) => draft.chatIds.delete(id));
      else inList.forEach((id) => draft.chatIds.add(id));
      haptic('light');
      setTab('chats');
      renderChatList();
      renderFolders();
    };
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
      if (draft.step === STEP_LAST) renderSummary();
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
  note.textContent = scanReport(result);
  note.hidden = false;
  haptic('success');
  await loadChats();
}

/** Что нашлось при сканировании — включая «Избранное».

    Число материалов здесь не для полноты: без него человек, у которого
    список пуст, не может отличить «Избранное пустое» от «его не
    прочитали». */
function scanReport(result) {
  return `Нашлось ${result.chats} `
    + plural(result.chats, 'чат', 'чата', 'чатов')
    + `, ${result.folders} `
    + plural(result.folders, 'папка', 'папки', 'папок')
    + `, в «Избранном» ${result.materials} `
    + plural(result.materials, 'сообщение', 'сообщения', 'сообщений') + '.';
}

/** Перечитать «Избранное» прямо со второго шага. */
async function rescanSaved() {
  const button = $('rescan-saved');
  const note = $('saved-note');
  const done = busy(button, 'Читаем «Избранное»…');
  note.hidden = true;
  const result = await api('/api/chats/scan', { account_id: draft.accountId });
  done();
  if (!result.ok) {
    note.textContent = result.error;
    note.hidden = false;
    haptic('error');
    return;
  }
  note.textContent = scanReport(result);
  note.hidden = false;
  haptic(result.materials ? 'success' : 'light');
  await loadMaterials(draft.accountId);
  renderMaterials($('material-list'), draft.savedIds, renderVariantCount, true);
}

async function createCampaign() {
  const button = $('step-next');
  fail('new-err', '');

  const done = busy(button, 'Запускаем…');
  const result = await api('/api/campaign/create', {
    account_id: draft.accountId,
    chat_ids: [...draft.chatIds],
    variants: collectVariants(),
    interval: draft.interval,
    pick: draft.pick,
  });
  done();
  if (!result.ok) {
    fail('new-err', result.error);
    return;
  }
  haptic('success');
  await refresh();
}


// --- админка -----------------------------------------------------------

/* Раздел виден только владельцу, и это не единственная защита: сервер
   проверяет id по подписанной initData и чужим отвечает 404. Прятать
   вкладку — удобство, а не безопасность. */

async function renderAdmin() {
  if (!state.is_admin) return;
  const result = await api('/api/admin/stats', {});
  if (!result.ok) return;
  const st = result.stats;
  fillStats($('admin-stats'), [
    ['Людей всего', st.users || 0],
    ['Заходили за сутки', st.active_day || 0],
    ['На пробном', st.on_trial || 0],
    ['С оплатой', st.paid || 0],
    ['Аккаунтов подключено', st.accounts || 0],
    ['Из них живых', st.accounts_ok || 0],
  ]);
}

async function adminFind() {
  const button = $('admin-find');
  const query = $('admin-query').value.trim();
  if (!query) return;

  const done = busy(button, 'Ищем…');
  const result = await api('/api/admin/find', { query });
  done();
  if (!result.ok) return alertBox(result.error);

  const box = $('admin-results');
  box.textContent = '';
  box.hidden = false;
  $('admin-card').hidden = true;

  if (!result.users.length) {
    const row = document.createElement('div');
    row.className = 'row';
    row.textContent = 'Никого не нашлось.';
    box.appendChild(row);
    return;
  }

  for (const person of result.users) {
    const row = document.createElement('button');
    row.className = 'row';
    const body = document.createElement('div');
    body.className = 'row-body';
    const title = document.createElement('div');
    title.className = 'row-title';
    title.textContent = person.name || 'Без имени';
    const note = document.createElement('div');
    note.className = 'row-note';
    note.textContent = (person.username ? '@' + person.username + ' · ' : '')
      + 'ID ' + person.id + ' · ' + (person.coins || 0) + ' ' + me.coin_name;
    body.append(title, note);
    row.append(body, icon('chevron'));
    row.onclick = () => adminOpen(person.id);
    box.appendChild(row);
  }
}

async function adminOpen(userId) {
  const result = await api('/api/admin/user', { id: userId });
  if (!result.ok) return alertBox(result.error);
  const card = result.card;

  const box = $('admin-card');
  box.textContent = '';
  box.hidden = false;

  const head = document.createElement('h2');
  head.className = 'section-title';
  head.textContent = (card.user.name || 'Без имени')
    + (card.user.username ? ' · @' + card.user.username : '');
  box.appendChild(head);

  const summary = document.createElement('div');
  summary.className = 'panel';
  const sub = card.subscription;
  fillStats(summary, [
    ['ID', card.user.user_id],
    ['Монет', card.user.coins || 0],
    ['Подписка', sub.kind === 'paid' ? 'оплачена, ' + sub.days_left + ' дн.'
      : sub.kind === 'trial' ? 'пробная, ' + sub.days_left + ' дн.'
      : 'кончилась'],
    ['Аккаунтов', card.accounts.length],
    ['Рассылок', card.campaigns.length],
  ]);
  box.appendChild(summary);

  // Выдать монеты или дни — то, ради чего админка и нужна.
  box.appendChild(sectionTitle('Помочь'));
  const actions = document.createElement('div');
  actions.className = 'panel';
  actions.appendChild(adminGrantRow(userId, 'Выдать монеты', 'coins',
    'Сколько монет начислить? Отрицательное число спишет.'));
  actions.appendChild(adminGrantRow(userId, 'Продлить подписку', 'days',
    'На сколько дней продлить? Отрицательное число сократит.'));
  box.appendChild(actions);

  if (card.accounts.length) {
    box.appendChild(sectionTitle('Аккаунты'));
    const panel = document.createElement('div');
    panel.className = 'panel';
    for (const account of card.accounts) {
      const row = document.createElement('div');
      row.className = 'row stat';
      const left = document.createElement('span');
      left.textContent = (account.name || account.phone)
        + (account.source === 'tdata' ? ' · tdata' : '');
      const right = document.createElement('span');
      right.textContent = account.status === 'ok' ? 'работает'
        : (account.note || 'не работает');
      row.append(left, right);
      panel.appendChild(row);
    }
    box.appendChild(panel);
  }

  if (card.campaigns.length) {
    box.appendChild(sectionTitle('Рассылки'));
    const panel = document.createElement('div');
    panel.className = 'panel';
    for (const campaign of card.campaigns) {
      const row = document.createElement('div');
      row.className = 'row';
      const body = document.createElement('div');
      body.className = 'row-body';
      const title = document.createElement('div');
      title.className = 'row-title';
      title.textContent = campaign.title;
      const note = document.createElement('div');
      note.className = 'row-note';
      note.textContent = campaign.status + ' · отправлено '
        + campaign.sent_ok + ' · ошибок ' + campaign.sent_err
        + (campaign.note ? ' · ' + campaign.note : '');
      body.append(title, note);
      row.appendChild(body);

      const stop = document.createElement('button');
      stop.className = 'btn small'
        + (campaign.status === 'running' ? ' danger' : '');
      stop.textContent = campaign.status === 'running' ? 'Стоп' : 'Пуск';
      stop.onclick = async () => {
        const answer = await api('/api/admin/campaign', {
          id: campaign.id,
          status: campaign.status === 'running' ? 'stopped' : 'running',
        });
        if (!answer.ok) return alertBox(answer.error);
        haptic('success');
        adminOpen(userId);
      };
      row.appendChild(stop);
      panel.appendChild(row);
    }
    box.appendChild(panel);
  }

  if (card.coins.length) {
    box.appendChild(sectionTitle('Монеты'));
    const panel = document.createElement('div');
    panel.className = 'panel';
    fillStats(panel, card.coins.map((op) => [
      op.reason, (op.delta > 0 ? '+' : '') + op.delta,
    ]));
    box.appendChild(panel);
  }

  if (card.invoices.length) {
    box.appendChild(sectionTitle('Счета'));
    const panel = document.createElement('div');
    panel.className = 'panel';
    fillStats(panel, card.invoices.map((inv) => [
      inv.coins + ' ' + me.coin_name + ' · ' + inv.status,
      Math.round(inv.rub),
    ]));
    box.appendChild(panel);
  }

  if (card.sends.length) {
    box.appendChild(sectionTitle('Последние отправки'));
    const panel = document.createElement('div');
    panel.className = 'panel';
    for (const send of card.sends) {
      const row = document.createElement('div');
      row.className = 'log-row';
      row.appendChild(icon(send.ok ? 'check' : 'x',
        'ic-s log-mark ' + (send.ok ? 'good' : 'bad')));
      const chat = document.createElement('span');
      chat.className = 'log-chat';
      chat.textContent = send.title || '—';
      if (!send.ok && send.error) {
        const error = document.createElement('div');
        error.className = 'log-error';
        error.textContent = send.error;
        chat.appendChild(error);
      }
      row.appendChild(chat);
      panel.appendChild(row);
    }
    box.appendChild(panel);
  }

  window.scrollTo(0, 0);
}

function sectionTitle(text) {
  const title = document.createElement('h2');
  title.className = 'section-title';
  title.textContent = text;
  return title;
}

function adminGrantRow(userId, title, field, question) {
  const row = document.createElement('button');
  row.className = 'row';
  const body = document.createElement('div');
  body.className = 'row-body';
  const name = document.createElement('div');
  name.className = 'row-title';
  name.textContent = title;
  body.appendChild(name);
  row.append(body, icon('chevron'));

  row.onclick = async () => {
    const raw = window.prompt(question, '');
    if (raw === null) return;
    const value = parseInt(String(raw).replace(/[^0-9-]/g, ''), 10);
    if (!value) return;
    const payload = { id: userId };
    payload[field] = value;
    const result = await api('/api/admin/grant', payload);
    if (!result.ok) return alertBox(result.error);
    haptic('success');
    alertBox(result.done);
    adminOpen(userId);
  };
  return row;
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

  $('admin-find').onclick = adminFind;
  $('admin-query').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') adminFind();
  });
  $('paid-limit-save').onclick = (event) => savePaidLimit(event.currentTarget);
  $('paid-limit').onkeydown = (event) => {
    if (event.key === 'Enter') savePaidLimit($('paid-limit-save'));
  };

  $('add-watch').onclick = () => openWatch(null);
  $('watch-save').onclick = (event) => saveWatch(event.currentTarget);
  $('watch-variant-add').onclick = () => {
    if (watching.texts.length >= state.limits.max_variants) return;
    watching.texts.push('');
    watching.attach.push(null);
    renderWatchVariants();
  };
  for (const tab of document.querySelectorAll('.wtab')) {
    tab.onclick = () => setWatchPick(tab.dataset.pick);
  }

  $('open-topup').onclick = openTopup;
  $('topup-custom').onclick = askTopupCustom;
  $('topup-own-apply').onclick = applyTopupCustom;
  $('topup-own-input').onkeydown = (event) => {
    if (event.key === 'Enter') applyTopupCustom();
  };
  $('topup-pay').onclick = payTopup;
  $('add-tdata').onclick = openTdata;
  $('tdata-send').onclick = uploadTdata;
  $('add-campaign').onclick = openNew;
  $('rescan').onclick = rescan;
  $('rescan-saved').onclick = rescanSaved;
  $('step-next').onclick = stepNext;
  $('step-back').onclick = stepBack;
  $('variant-add').onclick = addVariant;
  $('chat-search').addEventListener('input', renderChatList);
  for (const tab of document.querySelectorAll('.step')) {
    tab.onclick = () => jumpStep(Number(tab.dataset.step));
  }
  for (const tab of document.querySelectorAll('.ptab')) {
    tab.onclick = () => setPick(tab.dataset.pick);
  }
  $('campaign-account').addEventListener('change', () => {
    draft.accountId = Number($('campaign-account').value);
    // Чаты у каждого аккаунта свои: выбранное от прошлого аккаунта
    // здесь не просто лишнее, оно относится к чужому списку.
    // Чаты и материалы у каждого аккаунта свои: выбранное от прошлого
    // относится к чужому списку.
    draft.chatIds.clear();
    draft.savedIds.clear();
    loadChats();
  });
  for (const tab of document.querySelectorAll('.tab')) {
    tab.onclick = () => setTab(tab.dataset.tab);
  }
  for (const tab of document.querySelectorAll('.etab')) {
    tab.onclick = () => setEditPick(tab.dataset.pick);
  }
  for (const item of document.querySelectorAll('.tab-item')) {
    item.onclick = () => setPane(item.dataset.pane);
  }
  $('edit-save').onclick = saveEdit;
  $('edit-variant-add').onclick = () => {
    if (editing.texts.length >= state.limits.max_variants) return;
    editing.texts.push('');
    editing.attach.push(null);
    renderEditVariants();
  };

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
  // Шапка и фон клиента — в тон приложению. Без этого над светлым
  // стеклом висит чёрная полоса из тёмной темы Telegram.
  try {
    if (tg.setHeaderColor) tg.setHeaderColor('#f2f4f8');
    if (tg.setBackgroundColor) tg.setBackgroundColor('#eef0f5');
  } catch (error) { /* старый клиент — не беда */ }
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
