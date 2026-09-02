/* Perfboard Router - interface local */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  netlist: '',
  fileName: '',
  analysis: null,
  result: null,
  placements: [],      // [{ref,col,row,rot,locked}]
  pendingPlacements: null,   // posicoes de um .json antigo, esperando a netlist
  overrides: {},       // ref -> {margins:[esq,cima,dir,baixo]} ajustados por voce
  decoupling: [],      // pares capacitor/CI que devem ficar colados
  editing: null,       // ref com o editor de tamanho aberto
  aborto: null,        // AbortController do roteamento em curso
  melhorVisto: null,   // menor numero de pinos soltos ja alcancado nesta busca
  seed: 1,             // primeira semente; a busca ilimitada percorre as seguintes
  job: null,           // id da busca em curso, para poder pedir parada
  veuInicio: 0,
  pedidoPendente: null,      // roteamento pedido enquanto outro ainda rodava
  veuTimer: null,            // cronometro do veu de 'roteando'
  selected: null,
  busy: false,
};

/* ---------------- utilidades ---------------- */

function status(msg, cls) {
  const el = $('#status');
  el.textContent = msg;
  el.className = 'status' + (cls ? ' ' + cls : '');
}

// "Failed to fetch" nao diz nada para quem esta usando: traduz para o que fazer.
class ErroServidor extends Error {}

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  } catch (e) {
    if (location.protocol === 'file:') {
      throw new ErroServidor(
        'a página foi aberta direto do arquivo (file://). Ela precisa do servidor: ' +
        'rode "python server.py" e abra http://127.0.0.1:8765');
    }
    throw new ErroServidor(
      'não consegui falar com o servidor. Ele está rodando? ' +
      'Na pasta do projeto: python server.py');
  }

  if (res.status === 429) throw new ErroServidor('muitas requisições seguidas; espere alguns segundos');
  if (res.status === 503) throw new ErroServidor('servidor ocupado; tente de novo em alguns segundos');

  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new ErroServidor('o servidor respondeu algo que não é JSON (HTTP ' + res.status + ')');
  }
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

// Pede o solve em modo fluxo: o servidor manda uma linha JSON por evento e a
// ultima e o resultado. Sem isso a tela fica muda por minutos numa busca dificil.
async function apiFluxo(corpo, aoProgresso) {
  let res;
  try {
    res = await fetch('api/solve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ stream: true }, corpo)),
      signal: S.aborto ? S.aborto.signal : undefined,
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ErroServidor('nao consegui falar com o servidor. Ele esta rodando? '
      + 'Na pasta do projeto: python server.py');
  }
  if (res.status === 429) throw new ErroServidor('muitas requisicoes seguidas; espere alguns segundos');
  if (res.status === 503) throw new ErroServidor('servidor ocupado; tente de novo em alguns segundos');
  if (!res.ok || !res.body) {
    const txt = await res.text().catch(() => '');
    throw new Error(txt.slice(0, 300) || ('HTTP ' + res.status));
  }

  const leitor = res.body.getReader();
  const dec = new TextDecoder();
  let resto = '';
  let final = null;
  for (;;) {
    const { value, done } = await leitor.read();
    if (done) break;
    resto += dec.decode(value, { stream: true });
    let corte;
    while ((corte = resto.indexOf('\n')) >= 0) {
      const linha = resto.slice(0, corte).trim();
      resto = resto.slice(corte + 1);
      if (!linha) continue;
      let ev;
      try { ev = JSON.parse(linha); } catch (e) { continue; }
      if (ev.tipo === 'inicio') S.job = ev.job || null;
      else if (ev.tipo === 'progresso') aoProgresso(ev);
      else if (ev.tipo === 'final') final = ev.resultado;
      else if (ev.tipo === 'erro') throw new Error(ev.detail || ev.error || 'falha');
    }
  }
  if (!final) throw new Error('o servidor encerrou sem devolver resultado');
  return final;
}

// Traduz o evento cru numa frase que diz o que esta acontecendo agora.
function fraseProgresso(ev) {
  const t = !ev.tentativa ? ''
    : (ev.de_tentativas > 1 ? `tentativa ${ev.tentativa}/${ev.de_tentativas} · `
                            : (ev.de_tentativas === 0 ? `tentativa ${ev.tentativa} · ` : ''));
  switch (ev.fase) {
    case 'posicionando':
      return `${t}posicionando as peças…`;
    case 'negociando':
      return `${t}negociando cruzamentos · rodada ${ev.rodada}/${ev.de} · `
           + `${ev.disputados} conflito(s) restante(s)`;
    case 'legalizando':
      return `${t}resolvendo conflitos · rodada ${ev.rodada}/${ev.de} · `
           + `${ev.soltos} pino(s) sem ligação`;
    case 'tentativa_concluida': {
      if (!ev.ponto || !ev.ponto.fechou) return `${t}terminou com ${ev.soltos} pino(s) solto(s)`;
      if (ev.ponto.melhorou) return `${t}fechou 100% e ficou mais fácil de montar`;
      return `${t}${ev.fechadas} solução(ões) completa(s) · `
           + `${ev.sem_melhora}/${ev.paciencia} sem ganho`;
    }
    case 'plato':
      return 'a melhora estacionou — entregando o melhor';
    case 'bando':
      return ev.fase_detalhe === 'abrindo'
        ? `preparando ${ev.nucleos} processos, um por núcleo…`
        : `${ev.nucleos} tentativas ao mesmo tempo, uma por núcleo…`;
    case 'calibrando_jumpers':
      return `ajustando para caber em ${ev.limite} jumpers (está em ${ev.atual})…`;
    case 'interrompido':
      return 'parando — vou entregar o melhor que encontrei';
    case 'diagnosticando':
      return 'não fechou — descobrindo o que destravaria…';
    default:
      return 'trabalhando…';
  }
}

async function servidorVivo() {
  try {
    const r = await fetch('api/examples', { method: 'GET' });
    return r.ok;
  } catch (e) {
    return false;
  }
}

function download(name, text, type) {
  const blob = new Blob([text], { type: type || 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function holeLabel(col, row) {
  if ($('#labelStyle').value !== 'letra') return `(${col},${row})`;
  let s = '', n = row;
  if (row < 0) return `(${col},${row})`;
  for (;;) { s = String.fromCharCode(65 + (n % 26)) + s; n = Math.floor(n / 26) - 1; if (n < 0) break; }
  return s + (col + 1);
}

function netColor(name) {
  const n = (name || '').toUpperCase();
  if (n.includes('GND') || n.endsWith('VSS')) return '#3b3b3b';
  if (['VCC', 'VDD', '+5V', '+3V3', '+12V', 'VBUS', 'VIN'].some((k) => n.includes(k))) return '#d33';
  const pal = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#008080',
               '#f032e6', '#9a6324', '#808000', '#00a3a3', '#bc5090', '#5a8f29'];
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return pal[h % pal.length];
}

/* ---------------- leitura de parametros ---------------- */

function boardSpec() {
  return {
    cols: +$('#cols').value,
    rows: +$('#rows').value,
    margin_holes: +$('#margin').value,
  };
}

// Esforco sempre no maximo: com o posicionador em C uma tentativa custa décimos de
// segundo, entao economizar esforco so piora o resultado sem devolver tempo util.
const ESFORCO = 'alto';
// Paciencia = quantas solucoes COMPLETAS seguidas sem ganho antes de declarar que a
// busca estacionou. Tentativas que nem fecham nao contam: elas nao dizem nada sobre
// a qualidade ter parado de melhorar.
const PACIENCIA = 60;

function payload(autoPlace, keepExisting) {
  return {
    netlist: S.netlist,
    board: boardSpec(),
    placements: S.placements,
    overrides: S.overrides,
    decoupling: S.decoupling.filter((d) => d.enabled !== false),
    auto_place: autoPlace,
    scale: +$('#scale').value,
    label_style: $('#labelStyle').value,
    placer: {
      effort: ESFORCO,
      seed: S.seed,
      tries: 0,   // sempre sem limite: quem decide quando parar e o usuario
      modo: $('#modoBusca').value,
      paciencia: PACIENCIA,
      keep_existing: !!keepExisting,
      edge_pull: $('#edgePull').checked ? 0.6 : 0,
    },
    router: {
      preset: $('#preset').value,
      faces: +$('#faces').value,
      allow_jumpers: $('#allowJumpers').checked,
      max_jumper: +$('#maxJumper').value,
      // campo vazio = sem teto; com teto o solve faz varias passadas de calibragem
      max_jumpers: $('#maxJumpers').value === '' ? null : +$('#maxJumpers').value,
      attempts: 6,        // rodadas de rip-up: o valor que fecha mais circuitos
    },
  };
}

function updateFacesHint() {
  const duas = $('#faces').value === '2';
  $('#facesHint').innerHTML = duas
    ? `Cada furo tem duas ilhas independentes, sem metalização ligando uma na outra. Dá para
       fazer <b>trilha no lado dos componentes</b> e <b>via</b> (fio no furo, soldado dos dois
       lados). É assim que duas redes se cruzam sem encostar — normalmente dispensa jumper.`
    : `Cobre só de um lado. Solda pelo lado de cima escorre e encosta na ilha de baixo, então o
       lado dos componentes só aceita <b>jumper</b>: fio isolado que pousa só nas pontas.`;
}

function updateBoardMm() {
  const b = boardSpec();
  const w = ((b.cols - 1) * 2.54).toFixed(1);
  const h = ((b.rows - 1) * 2.54).toFixed(1);
  $('#boardmm').textContent = `${b.cols} x ${b.rows} furos ≈ ${w} x ${h} mm (${b.cols * b.rows} furos)`;
}

function currentSettings() {
  return {
    cols: +$('#cols').value, rows: +$('#rows').value, margin: +$('#margin').value,
    scale: +$('#scale').value, labelStyle: $('#labelStyle').value,
    effort: ESFORCO, seed: S.seed,
    edgePull: $('#edgePull').checked,
    modoBusca: $('#modoBusca').value,
    faces: $('#faces').value,
    preset: $('#preset').value,
    allowJumpers: $('#allowJumpers').checked, maxJumper: +$('#maxJumper').value,
    maxJumpers: $('#maxJumpers').value,
  };
}

function applySettings(cfg) {
  if (!cfg) return;
  const num = { cols: 'cols', rows: 'rows', margin: 'margin', scale: 'scale',
                maxJumper: 'maxJumper' };
  if (typeof cfg.seed === 'number') S.seed = cfg.seed;
  for (const [k, id] of Object.entries(num)) {
    if (typeof cfg[k] === 'number') $('#' + id).value = cfg[k];
  }
  if (cfg.maxJumpers !== undefined) $('#maxJumpers').value = cfg.maxJumpers;
  const sel = { labelStyle: 'labelStyle', faces: 'faces',
                preset: 'preset', modoBusca: 'modoBusca' };
  for (const [k, id] of Object.entries(sel)) {
    if (cfg[k] !== undefined) $('#' + id).value = String(cfg[k]);
  }
  const chk = { edgePull: 'edgePull', allowJumpers: 'allowJumpers' };
  for (const [k, id] of Object.entries(chk)) {
    if (typeof cfg[k] === 'boolean') $('#' + id).checked = cfg[k];
  }
  updateBoardMm();
  updateFacesHint();
}

/* ---------------- curva da busca ---------------- */

// O que a busca persegue depois de fechar 100%: reduzir o TRABALHO DE MONTAGEM.
// Cada dobra do fio, cada via e cada jumper é serviço de bancada, e o peso de cada
// um vem do perfil escolhido em "Onde economizar" — o mesmo peso que guiou o
// roteamento. Otimizar com um critério e ranquear com outro não faria sentido.
//
// O gráfico mostra isso como uma linha que SOBE, porque é progresso:
//   antes de fechar  -> % do circuito já ligado, subindo até 100%
//   depois de fechar -> % de trabalho economizado em relação à PRIMEIRA solução
//                       completa, subindo enquanto ele acha montagens mais fáceis
function serieBusca(hist) {
  const fechou = hist.some((p) => p.fechou);
  const pts = [];
  let melhor = -Infinity;
  for (const p of hist) {
    const v = fechou ? (p.fechou ? p.economia : null) : p.ligado;
    if (v !== null && v !== undefined) melhor = Math.max(melhor, v);
    if (melhor > -Infinity) pts.push({ x: p.tentativa, best: melhor, v, p });
  }
  return {
    pts,
    fechou,
    rotulo: fechou ? 'trabalho de montagem economizado' : 'circuito já ligado',
  };
}

function desenhaCurva(hist, opts) {
  const o = Object.assign({ w: 380, h: 150, eixos: true }, opts || {});
  if (!hist || hist.length < 2) return '';
  const { pts, fechou, rotulo } = serieBusca(hist);
  if (pts.length < 2) return '';

  const padL = o.eixos ? 34 : 2;
  const padR = 2;
  const padT = 10;
  const padB = o.eixos ? 16 : 2;
  const x0 = pts[0].x;
  const x1 = pts[pts.length - 1].x;
  // O eixo começa onde a curva começa e sobra um respiro em cima, senão a linha
  // encosta no teto e some.
  const lo = fechou ? 0 : Math.min(...pts.map((q) => q.best));
  const alto = Math.max(...pts.map((q) => q.best));
  const hi = alto > lo ? alto + (alto - lo) * 0.12 : lo + 1;
  const px = (x) => padL + (x1 === x0 ? 0 : (x - x0) / (x1 - x0)) * (o.w - padL - padR);
  const py = (v) => padT + (1 - (v - lo) / (hi - lo)) * (o.h - padT - padB);

  // a melhor-até-agora só sobe, nunca desce: degraus, não rampa
  const d = [];
  pts.forEach((q, i) => {
    if (i === 0) { d.push('M' + px(q.x).toFixed(1) + ',' + py(q.best).toFixed(1)); return; }
    d.push('L' + px(q.x).toFixed(1) + ',' + py(pts[i - 1].best).toFixed(1));
    d.push('L' + px(q.x).toFixed(1) + ',' + py(q.best).toFixed(1));
  });
  const area = d.join('') + 'L' + px(x1).toFixed(1) + ',' + py(lo).toFixed(1)
             + 'L' + px(x0).toFixed(1) + ',' + py(lo).toFixed(1) + 'Z';

  const r = o.eixos ? 2.6 : 1.7;
  const bolas = pts.filter((q) => q.v !== null && q.v !== undefined && q.p.melhorou)
    .map((q) => `<circle cx="${px(q.x).toFixed(1)}" cy="${py(q.v).toFixed(1)}" r="${r}"
      fill="var(--ok)"/>`).join('');

  // faixa do platô: da última subida até o fim
  let ult = 0;
  pts.forEach((q, i) => { if (i && q.best > pts[i - 1].best) ult = i; });
  let faixa = '';
  if (ult && ult < pts.length - 1) {
    const a = px(pts[ult].x);
    const b = px(pts[pts.length - 1].x);
    faixa = `<rect x="${a.toFixed(1)}" y="${padT}" width="${(b - a).toFixed(1)}"
      height="${(o.h - padT - padB).toFixed(1)}" fill="var(--dim)" opacity=".10"/>`
      + (o.eixos ? `<text x="${((a + b) / 2).toFixed(1)}" y="${padT + 10}"
        text-anchor="middle" font-size="9" fill="var(--dim)">estacionou</text>` : '');
  }

  let eixos = '';
  if (o.eixos) {
    const pc = (v) => Math.round(v) + '%';
    eixos = `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${o.h - padB}" stroke="var(--line)"/>
      <line x1="${padL}" y1="${o.h - padB}" x2="${o.w - padR}" y2="${o.h - padB}" stroke="var(--line)"/>
      <text x="${padL - 5}" y="${py(alto) + 3}" text-anchor="end" font-size="9"
        fill="var(--ok)">${pc(alto)}</text>
      <text x="${padL - 5}" y="${o.h - padB}" text-anchor="end" font-size="9"
        fill="var(--dim)">${pc(lo)}</text>
      <text x="${padL}" y="${o.h - 4}" font-size="9" fill="var(--dim)">tentativa ${x0}</text>
      <text x="${o.w - padR}" y="${o.h - 4}" text-anchor="end" font-size="9"
        fill="var(--dim)">${x1}</text>`;
  }

  return `<svg class="curva-svg" viewBox="0 0 ${o.w} ${o.h}" role="img"
    aria-label="curva de ${rotulo} por tentativa">${faixa}${eixos}
    <path d="${area}" fill="var(--accent2)" opacity=".13"/>
    <path d="${d.join('')}" fill="none" stroke="var(--accent2)" stroke-width="2"
      stroke-linejoin="round"/>
    ${bolas}</svg>`;
}

/* ---------------- ciclo principal ---------------- */

async function loadNetlist(text, name, placements) {
  S.netlist = text;
  S.fileName = name || 'netlist.net';
  S.placements = placements || [];
  S.selected = null;
  S.editing = null;
  if (!placements) S.overrides = {};
  status('lendo netlist...', 'busy');
  mostraVeu('Lendo a netlist…');
  try {
    S.analysis = await api('api/analyze', { netlist: text });
  } catch (e) {
    escondeVeu();
    status('erro: ' + e.message, 'bad');
    return;
  }
  if (!placements) {
    const sb = S.analysis.suggested_board;
    $('#cols').value = sb.cols;
    $('#rows').value = sb.rows;
  }
  updateBoardMm();
  S.decoupling = (S.pendingDecoupling || S.analysis.decoupling || [])
    .map((d) => Object.assign({ enabled: true }, d));
  S.pendingDecoupling = null;
  preencheSelectsDecoup();
  const s = S.analysis.summary;
  $('#srcinfo').innerHTML =
    `<b>${S.fileName}</b><br>${s.components} componentes · ${s.pins} pinos · ` +
    `${s.nets} redes (${s.routable_nets} a rotear)`;

  // com posições vindas de um projeto salvo, não reposiciona: só reroteia
  await run(!placements, false);
}

// Veu com spinner sobre o desenho. Fica visivel enquanto roteia porque o resultado
// na tela ainda e o anterior; sem isso parece que nada esta acontecendo.
function mostraVeu(texto) {
  const el = $('#overlay');
  $('#overlayTxt').textContent = texto;
  $('#overlaySub').textContent = 'começando…';
  $('#overlayPlacar').textContent = '';
  $('#overlayCurva').innerHTML = '';
  $('#overlayMedida').innerHTML = '';
  $('#overlayNota').innerHTML = '';
  el.hidden = false;
  S.veuInicio = performance.now();
  clearInterval(S.veuTimer);
  S.veuTimer = setInterval(() => {
    const s = (performance.now() - S.veuInicio) / 1000;
    $('#overlayTempo').textContent = s < 60
      ? `${s.toFixed(0)} s`
      : `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, '0')} s`;
  }, 250);
}

// Chamado a cada evento do servidor: e isto que faz a espera deixar de ser cega.
function atualizaVeu(frase, melhor) {
  $('#overlaySub').textContent = frase;
  const el = $('#overlayPlacar');
  if (typeof melhor === 'number') {
    el.textContent = melhor === 0
      ? 'melhor até agora: tudo ligado ✓'
      : `melhor até agora: ${melhor} pino(s) sem ligação`;
    el.className = 'overlay-placar ' + (melhor === 0 ? 'bom' : 'ruim');
  } else {
    el.textContent = '';
  }
}

// Uma linha subindo, sozinha, nao quer dizer nada. Aqui fica escrito o que ela mede,
// quanto vale agora e o que ele esta procurando - senao a espera volta a ser cega.
function legendaVeu(ev) {
  const ponto = ev.ponto;
  const fechou = S.historico.some((p) => p.fechou);
  const medida = $('#overlayMedida');
  const nota = $('#overlayNota');

  if (!fechou) {
    medida.innerHTML = `circuito ligado <b>${Math.round(ponto.ligado)}%</b>`;
    nota.innerHTML = 'a linha sobe conforme sobram menos pinos sem ligação. '
      + 'Ainda não achou nenhuma solução 100% completa.';
    return;
  }

  const m = S.melhorPonto;
  const eco = m && m.economia ? Math.round(m.economia) : 0;
  medida.innerHTML = 'trabalho de montagem '
    + (eco > 0 ? `<b class="ganho">−${eco}%</b>` : '<b>−0%</b>');
  const peças = m
    ? `${m.quinas} quinas · ${m.vias} vias · ${m.jumpers} jumpers · ${Math.round(m.mm)} mm`
    : '';
  nota.innerHTML = `melhor montagem até agora: ${peças}.<br>`
    + (eco > 0
       ? `Já são ${eco}% menos serviço que a primeira que fechou. `
       : 'Ainda no mesmo trabalho da primeira que fechou. ')
    + `A linha sobe quando ele acha uma montagem mais fácil — `
    + `${ev.sem_melhora ?? 0}/${ev.paciencia ?? '?'} tentativas completas sem ganho.`;
}

function escondeVeu() {
  clearInterval(S.veuTimer);
  S.veuTimer = null;
  S.aborto = null;
  S.job = null;
  $('#overlayParar').disabled = false;
  $('#overlay').hidden = true;
}

async function run(autoPlace, keepExisting) {
  if (!S.netlist) return;
  if (S.busy) {
    // arrastou outra peca enquanto ainda roteava: guarda o pedido em vez de ignorar
    S.pedidoPendente = { autoPlace, keepExisting };
    return;
  }
  S.busy = true;
  document.body.classList.add('roteando');
  mostraVeu(autoPlace ? 'Posicionando e roteando…' : 'Roteando…');
  status(autoPlace ? 'posicionando e roteando...' : 'roteando...', 'busy');
  const t0 = performance.now();
  try {
    S.aborto = new AbortController();
    S.melhorVisto = null;
    S.historico = [];
    S.melhorPonto = null;
    S.result = await apiFluxo(payload(autoPlace, keepExisting), (ev) => {
      if (ev.ponto) {
        S.historico.push(ev.ponto);
        if (ev.ponto.melhorou) S.melhorPonto = ev.ponto;
        // pequeno e sem eixos: a forma vem do desenho, o significado vem do texto
        $('#overlayCurva').innerHTML = desenhaCurva(S.historico, { w: 240, h: 54, eixos: false });
        legendaVeu(ev);
      }
      if (typeof ev.melhor === 'number'
          && (S.melhorVisto === null || ev.melhor < S.melhorVisto)) {
        S.melhorVisto = ev.melhor;
      }
      if (typeof ev.soltos === 'number' && ev.fase === 'tentativa_concluida'
          && (S.melhorVisto === null || ev.soltos < S.melhorVisto)) {
        S.melhorVisto = ev.soltos;
      }
      atualizaVeu(fraseProgresso(ev), S.melhorVisto);
    });
    S.placements = S.result.layout.placements;
    draw();
    const st = S.result.stats;
    const dt = ((performance.now() - t0) / 1000).toFixed(1);
    const soltos = (st.orphan_pins || []).length;
    if (st.nets_failed === 0 && !S.result.shorts.length) {
      status(`ok — ${st.nets_routed} redes · ${st.operacoes ?? '?'} operações `
             + `(${st.quinas ?? '?'} quinas, ${st.vias || 0} vias, ${st.jumpers} jumpers) `
             + `· ${st.total_mm} mm (${dt}s)`, 'ok');
    } else {
      const partes = [];
      if (S.result.shorts.length) partes.push(`${S.result.shorts.length} curto(s) entre pinos`);
      if (st.nets_failed) partes.push(`${st.nets_failed} rede(s) incompleta(s)`);
      if (soltos) partes.push(`${soltos} pino(s) sem ligação`);
      if (S.result.problems.length) partes.push(`${S.result.problems.length} problema(s) de posição`);
      status(`${partes.join(', ') || 'layout com pendências'} (${dt}s)`, 'bad');
    }
  } catch (e) {
    if (e.name === 'AbortError') status('interrompido por você', 'bad');
    else status('erro: ' + e.message, 'bad');
  } finally {
    S.busy = false;
    document.body.classList.remove('roteando');
    const pendente = S.pedidoPendente;
    S.pedidoPendente = null;
    if (pendente) run(pendente.autoPlace, pendente.keepExisting);
    else escondeVeu();   // so tira o veu quando nao ha mais nada na fila
  }
}

/* ---------------- desenho ---------------- */

function draw() {
  const r = S.result;
  if (!r) return;
  $('#viewTop .svgbox').innerHTML = r.svg_top;
  $('#viewBottom .svgbox').innerHTML = r.svg_bottom;
  wireDrag($('#viewTop .svgbox svg'));
  renderStats();
  renderIssues();
  renderComponents();
  renderDecoup();
  renderBuild();
  markSelection();
  guardaSessao();
}

function renderStats() {
  const st = S.result.stats;
  const b = S.result.board;
  $('#stats').innerHTML = `
    <h2>Resultado</h2>
    <div class="statgrid">
      <div><b>${st.nets_routed}/${st.nets_routed + st.nets_failed}</b><span>redes roteadas</span></div>
      <div><b>${st.operacoes ?? '-'}</b><span>operações manuais</span></div>
      <div><b>${st.quinas ?? '-'}</b><span>quinas (dobras)</span></div>
      <div><b>${st.corridas ?? '-'}</b><span>trechos retos</span></div>
      <div><b>${st.jumpers}</b><span>jumpers por cima</span></div>
      <div><b>${st.vias || 0}</b><span>vias (troca de face)</span></div>
      <div><b>${st.top_trace_mm || 0} mm</b><span>trilha lado componentes</span></div>
      <div><b>${st.trace_mm} mm</b><span>trilha na solda</span></div>
      <div><b>${st.jumper_mm} mm</b><span>fio de jumper</span></div>
      <div><b>${st.holes_used}</b><span>furos usados</span></div>
      <div><b style="color:${(st.orphan_pins || []).length ? '#ff6b6b' : '#58c98a'}">${(st.orphan_pins || []).length}</b><span>pinos sem ligação</span></div>
      <div><b>${b.width_mm} × ${b.height_mm} mm</b><span>placa</span></div>
    </div>`;
}

function renderIssues() {
  const r = S.result;
  const out = [];
  for (const s of r.shorts) {
    out.push(`<div class="msg err">Curto no furo (${s.cell}): ${s.a} e ${s.b} caem no mesmo furo.</div>`);
  }
  for (const p of r.problems) out.push(`<div class="msg err">${p}</div>`);
  const lim = r.jumper_limit;
  if (lim && lim.mensagem) {
    out.push(`<div class="msg ${lim.atingido ? 'warn' : 'err'}">${lim.mensagem}.</div>`);
  }
  if (r.suggestion) {
    out.push(`<div class="msg warn"><b>${r.suggestion.message}</b></div>`);
  }
  const orphans = r.stats.orphan_pins || [];
  if (orphans.length) {
    const lista = orphans
      .map((o) => `<li><b>${o.ref}.${o.pin}</b> no furo <b>${o.label}</b> — rede ${o.net}</li>`)
      .join('');
    out.push(`<div class="msg err"><b>${orphans.length} pino(s) sem ligação real.</b>
      No desenho eles estão marcados com <b>X vermelho</b>, e os trechos da rede que não fechou
      aparecem com casca vermelha tracejada.<ul>${lista}</ul>
      Saídas: ligar o jumper à mão, permitir jumpers, aumentar a placa, ou tentar outra semente.</div>`);
  }
  for (const f of r.stats.failed) {
    const why = (r.routes.find((x) => x.name === f) || {}).reason || '';
    out.push(`<div class="msg err">Rede <b>${f}</b> não fechou. ${why}</div>`);
  }
  for (const w of r.warnings) out.push(`<div class="msg warn">${w}</div>`);
  for (const s of r.stats.skipped) {
    out.push(`<div class="msg warn">Rede ${s} ignorada: menos de 2 pinos posicionados.</div>`);
  }
  if (!out.length) out.push('<div class="msg ok">Sem conflitos: layout fisicamente realizável.</div>');
  $('#issues').innerHTML = '<h2>Verificação</h2>' + out.join('');
}

function renderComponents() {
  const fps = S.result.layout.footprints;
  const html = S.placements
    .slice()
    .sort((a, b) => a.ref.localeCompare(b.ref, 'pt', { numeric: true }))
    .map((p) => {
      const fp = fps[p.ref] || {};
      const mm = fp.body_mm ? `${fp.body_mm[0]}×${fp.body_mm[1]} mm` : (fp.label || '');
      const ajustado = !!S.overrides[p.ref];
      const linha = `<div class="crow${S.selected === p.ref ? ' sel' : ''}" data-ref="${p.ref}">
        <b>${p.ref}</b>
        <span class="meta" title="${fp.key || ''}">${mm} · ${holeLabel(p.col, p.row)} · ${p.rot}°</span>
        <button data-act="size" class="${ajustado ? 'on' : ''}" title="tamanho real da peça">⤡</button>
        <button data-act="rot" title="girar 90°">⟳</button>
        <button data-act="lock" class="${p.locked ? 'on' : ''}" title="travar posição">${p.locked ? '🔒' : '🔓'}</button>
      </div>`;
      return linha + (S.editing === p.ref ? editorTamanho(p.ref, fp) : '');
    })
    .join('');
  $('#complist').innerHTML = html;
}

// Editor do tamanho real da peça: quantos furos o corpo passa dos pinos em cada
// lado. O footprint do KiCad acerta os terminais, mas não diz o volume do bicho -
// um borne Phoenix avança bem para trás dos parafusos.
function editorTamanho(ref, fp) {
  const m = (S.overrides[ref] && S.overrides[ref].margins) || fp.margins || [0, 0, 0, 0];
  const campo = (i, titulo) =>
    `<input type="number" min="0" max="30" value="${m[i]}" data-margin="${i}" title="${titulo}">`;
  const medida = fp.body_mm ? `${fp.body_mm[0]}<br>×<br>${fp.body_mm[1]} mm` : '';
  return `<div class="sizer" data-ref="${ref}">
    <div class="sizer-help">Furos que o corpo ocupa <b>além dos pinos</b>, em cada lado,
      na orientação original da peça. Gire depois com ⟳.</div>
    <div class="sizer-grid">
      <span></span>${campo(1, 'para cima')}<span></span>
      ${campo(0, 'para a esquerda')}<span class="sizer-mid">${medida}</span>${campo(2, 'para a direita')}
      <span></span>${campo(3, 'para baixo')}<span></span>
    </div>
    ${fp.body_note ? `<div class="sizer-note">${fp.body_note}</div>` : ''}
    <div class="row wrap">
      <button data-act="size-apply" data-ref="${ref}" class="ghost">Aplicar</button>
      <button data-act="size-reset" data-ref="${ref}" class="ghost">Voltar ao deduzido</button>
      <button data-act="size-close" class="ghost">Fechar</button>
    </div>
  </div>`;
}

function renderDecoup() {
  const achado = {};
  for (const d of (S.result && S.result.decoupling) || []) achado[d.cap + '>' + d.ic] = d;

  if (!S.decoupling.length) {
    $('#decoup').innerHTML =
      `<div class="hint">Nenhum par detectado. Um capacitor de desacoplamento é um capacitor
       de 2 pinos ligado entre a alimentação e o terra do mesmo CI. Se o seu não foi
       reconhecido, vincule à mão abaixo.</div>`;
    return;
  }

  $('#decoup').innerHTML = S.decoupling.map((d, i) => {
    const r = achado[d.cap + '>' + d.ic];
    let dist = '<span class="dist">—</span>';
    if (r && r.laco_mm !== undefined) {
      const cls = r.ok ? 'bom' : 'ruim';
      const piso = r.piso_mm > 0 ? ` (teórico ${r.piso_mm})` : '';
      dist = `<span class="dist ${cls}" title="alimentação ${r.pwr_mm} mm, terra ${r.gnd_mm} mm">`
           + `laço ${r.laco_mm} mm${piso}</span>`;
    } else if (d.enabled === false) {
      dist = '<span class="dist">desligado</span>';
    }
    return `<div class="dec">
      <input type="checkbox" data-dec="${i}" ${d.enabled === false ? '' : 'checked'}
             title="exigir proximidade">
      <span class="par"><b>${d.cap}</b> junto de <b>${d.ic}</b>
        <span class="hint" style="margin:0">(${d.net_pwr} / ${d.net_gnd})</span></span>
      ${dist}
    </div>`;
  }).join('') +
  `<div class="hint">Distância mostrada: alimentação / terra. O que importa é a
   <b>área do laço</b> ${'\u2014'} quanto menor, melhor o desacoplamento.</div>`;
}

function preencheSelectsDecoup() {
  if (!S.analysis) return;
  const caps = S.analysis.components.filter((c) => c.pins.length === 2);
  const cis = S.analysis.components.filter((c) => c.pins.length >= 5);
  const enche = (sel, lista) => {
    sel.innerHTML = lista.map((c) => `<option value="${c.ref}">${c.ref}</option>`).join('');
  };
  enche($('#decoupCap'), caps);
  enche($('#decoupIc'), cis);
}

// Descobre, pelas redes, quais pinos do par ficam na alimentação e no terra.
function montaPar(cap, ic) {
  const redeDe = {};
  for (const n of S.analysis.nets) for (const no of n.nodes) redeDe[no] = n.name;
  const ehTerra = (n) => /GND|VSS|0V/i.test(n);
  const ehPwr = (n) => !ehTerra(n) && /VCC|VDD|VEE|V\+|\+?\d+V\d*|VBUS|VIN|VS/i.test(n);

  const capC = S.analysis.components.find((c) => c.ref === cap);
  const icC = S.analysis.components.find((c) => c.ref === ic);
  if (!capC || !icC) return null;

  let pwr = null, gnd = null;
  for (const p of capC.pins) {
    const rede = redeDe[cap + '.' + p];
    if (!rede) continue;
    if (ehTerra(rede)) gnd = { pin: p, net: rede };
    else if (ehPwr(rede) || !pwr) pwr = { pin: p, net: rede };
  }
  if (!pwr || !gnd) return null;

  const pinoDoIc = (rede) => icC.pins.find((p) => redeDe[ic + '.' + p] === rede);
  const icPwr = pinoDoIc(pwr.net), icGnd = pinoDoIc(gnd.net);
  if (!icPwr || !icGnd) return null;

  return { cap, ic, enabled: true, auto: false,
           cap_pin_pwr: pwr.pin, ic_pin_pwr: icPwr, net_pwr: pwr.net,
           cap_pin_gnd: gnd.pin, ic_pin_gnd: icGnd, net_gnd: gnd.net };
}

function renderBuild() {
  const b = S.result.build;
  const t = b.totals;
  const dot = (n) => `<span class="netdot" style="background:${netColor(n)}"></span>`;
  const tops = (b.top_bridges || []).map((x) =>
    `<li>${dot(x.net)}<b>${x.net}</b>: ${x.from_label} → ${x.to_label} — ${x.holes} furos, ${x.length_mm} mm</li>`).join('');
  const vias = (b.vias || []).map((x) =>
    `<li>${dot(x.net)}<b>${x.net}</b>: furo ${x.from_label}</li>`).join('');
  const bridges = b.bridges.map((x) =>
    `<li>${dot(x.net)}<b>${x.net}</b>: ${x.from_label} → ${x.to_label} — ${x.holes} furos, ${x.length_mm} mm</li>`).join('');
  const jumpers = b.jumpers.map((x) =>
    `<li>${dot(x.net)}<b>${x.net}</b>: ${x.from_label} → ${x.to_label} — corte ${x.cut_mm} mm</li>`).join('');
  $('#build').innerHTML = `
    <div class="msg warn">${t.bridges} trilhas no lado da solda · ${t.jumpers} jumpers ·
      ${t.wire_cut_mm} mm de fio isolado a cortar</div>
    <h3>Vias — fio atravessando o furo, soldado nas duas faces</h3>
    <ol>${vias || '<li>nenhuma</li>'}</ol>
    <h3>Trilhas (lado da solda)</h3>
    <ol>${bridges || '<li>nenhuma</li>'}</ol>
    <h3>Trilhas (lado dos componentes)</h3>
    <ol>${tops || '<li>nenhuma</li>'}</ol>
    <h3>Jumpers — fio isolado sobrevoando</h3>
    <ol>${jumpers || '<li>nenhum</li>'}</ol>`;
}

function markSelection() {
  $$('#views .comp').forEach((g) => g.classList.toggle('sel', g.dataset.ref === S.selected));
  $$('.crow').forEach((c) => c.classList.toggle('sel', c.dataset.ref === S.selected));
}

/* ---------------- arrastar componentes ---------------- */

function svgHole(svg, evt) {
  const pad = +svg.dataset.pad, sc = +svg.dataset.scale, cols = +svg.dataset.cols;
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX; pt.y = evt.clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  let col = (p.x - pad) / sc;
  if (svg.dataset.side === 'bottom') col = cols - 1 - col;
  return { col: Math.round(col), row: Math.round((p.y - pad) / sc) };
}

function placementOf(ref) {
  return S.placements.find((p) => p.ref === ref);
}

function wireDrag(svg) {
  if (!svg) return;
  svg.addEventListener('pointerdown', (e) => {
    const g = e.target.closest('.comp');
    if (!g) { S.selected = null; markSelection(); return; }
    const ref = g.dataset.ref;
    S.selected = ref;
    markSelection();

    const pl = placementOf(ref);
    if (!pl || pl.locked) return;

    const start = svgHole(svg, e);
    const orig = { col: pl.col, row: pl.row };
    const sc = +svg.dataset.scale;
    const mirror = svg.dataset.side === 'bottom' ? -1 : 1;
    g.classList.add('dragging');
    svg.setPointerCapture(e.pointerId);

    const move = (ev) => {
      const cur = svgHole(svg, ev);
      const dc = cur.col - start.col, dr = cur.row - start.row;
      pl.col = orig.col + dc;
      pl.row = orig.row + dr;
      g.setAttribute('transform', `translate(${dc * sc * mirror},${dr * sc})`);
    };
    const up = () => {
      svg.removeEventListener('pointermove', move);
      svg.removeEventListener('pointerup', up);
      g.classList.remove('dragging');

      const mudou = pl.col !== orig.col || pl.row !== orig.row;
      if (!mudou) {
        g.removeAttribute('transform');
        return;
      }

      // NAO tira o deslocamento aqui: se tirasse, a peca voltaria visualmente para a
      // posicao do desenho antigo e so pularia para a nova quando o roteamento
      // terminasse - parece que o arrasto falhou. O transform fica ate o SVG novo
      // substituir este por inteiro.
      g.classList.add('pendente');
      renderComponents();
      markSelection();
      if ($('#autoReroute').checked) run(false, false);
    };
    svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerup', up);
  });
}

/* ---------------- sessão guardada no navegador ---------------- */

// Recarregar a página não pode custar o trabalho de posicionar as peças. Guardamos
// o projeto no navegador a cada mudança e restauramos sozinho na volta.
// Só o essencial vai para o armazenamento: netlist, ajustes, posições, tamanhos e
// desacoplamento. Rotas, SVG e guia de montagem são recalculados em ~0,1 s e
// ocupariam megabytes à toa.
const CHAVE_SESSAO = 'perfboard.sessao.v1';

function projetoCompacto() {
  if (!S.netlist) return null;
  return {
    format: 2,
    saved_at: new Date().toISOString(),
    fileName: S.fileName,
    netlist: S.netlist,
    settings: currentSettings(),
    overrides: S.overrides,
    decoupling: S.decoupling,
    layout: { placements: S.placements },
  };
}

let _guardaTimer = null;
function guardaSessao() {
  clearTimeout(_guardaTimer);
  _guardaTimer = setTimeout(() => {
    const proj = projetoCompacto();
    if (!proj) return;
    try {
      localStorage.setItem(CHAVE_SESSAO, JSON.stringify(proj));
      marcaSessao(proj.saved_at);
    } catch (e) {
      // cota estourada ou navegação privativa: seguir sem guardar, mas avisar
      marcaSessao(null, 'não consegui guardar no navegador (' + e.name + ')');
    }
  }, 400);
}

function marcaSessao(quando, erro) {
  const el = $('#sessaoInfo');
  if (!el) return;
  if (erro) { el.innerHTML = `<span class="aviso">${erro}</span>`; return; }
  if (!quando) { el.textContent = ''; return; }
  const h = new Date(quando).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  el.innerHTML = `guardado às ${h} · <a href="#" id="descartarSessao">descartar</a>`;
}

function leSessao() {
  try {
    const cru = localStorage.getItem(CHAVE_SESSAO);
    if (!cru) return null;
    const obj = JSON.parse(cru);
    return (obj && obj.netlist) ? obj : null;
  } catch (e) {
    return null;
  }
}

function descartaSessao() {
  try { localStorage.removeItem(CHAVE_SESSAO); } catch (e) { /* nada a fazer */ }
  marcaSessao(null);
}

/* ---------------- abrir projeto salvo ---------------- */

function placementsDoProjeto(obj) {
  const lista = (obj.layout && obj.layout.placements) || obj.placements;
  if (!Array.isArray(lista)) return null;
  const limpo = lista
    .filter((p) => p && typeof p.ref === 'string')
    .map((p) => ({
      ref: p.ref,
      col: Math.round(+p.col) || 0,
      row: Math.round(+p.row) || 0,
      rot: ((Math.round(+p.rot) || 0) % 360 + 360) % 360,
      locked: !!p.locked,
    }));
  return limpo.length ? limpo : null;
}

async function loadProject(obj, name) {
  const placements = placementsDoProjeto(obj);
  S.overrides = (obj.overrides && typeof obj.overrides === 'object') ? obj.overrides : {};
  if (Array.isArray(obj.decoupling)) S.pendingDecoupling = obj.decoupling;
  if (!placements) {
    status('esse JSON não tem posições de componentes', 'bad');
    return;
  }

  if (obj.settings) {
    applySettings(obj.settings);
  } else if (obj.board) {
    // formato antigo: pelo menos o tamanho da placa vem junto
    if (obj.board.cols) $('#cols').value = obj.board.cols;
    if (obj.board.rows) $('#rows').value = obj.board.rows;
    if (obj.board.margin_holes !== undefined) $('#margin').value = obj.board.margin_holes;
    updateBoardMm();
  }

  if (obj.netlist) {
    await loadNetlist(obj.netlist, obj.fileName || name.replace(/\.json$/i, '.net'), placements);
    return;
  }

  // JSON salvo no formato antigo: as posições estão aqui, a netlist não.
  if (S.netlist) {
    S.placements = placements;
    S.selected = null;
    status('posições restauradas — roteando...', 'busy');
    await run(false, false);
    return;
  }

  S.pendingPlacements = placements;
  status('agora carregue o .net deste projeto', 'bad');
  $('#srcinfo').innerHTML =
    `<div class="msg warn"><b>Posições carregadas de ${name}.</b><br>
     Esse JSON foi salvo num formato antigo, que não guardava a netlist.
     Carregue agora o <b>.net</b> do mesmo projeto e as ${placements.length} posições
     serão aplicadas automaticamente.</div>`;
}

function abrirArquivo(f) {
  if (!f) return;
  const ehJson = /\.json$/i.test(f.name);
  f.text().then((t) => {
    if (ehJson) {
      let obj;
      try {
        obj = JSON.parse(t);
      } catch (e) {
        status('JSON inválido: ' + e.message, 'bad');
        return;
      }
      return loadProject(obj, f.name);
    }
    const pendentes = S.pendingPlacements;
    S.pendingPlacements = null;
    return loadNetlist(t, f.name, pendentes);
  });
}

/* ---------------- exportacao ---------------- */

function buildText() {
  const r = S.result, b = r.build;
  const L = [];
  L.push('PERFBOARD - GUIA DE MONTAGEM');
  L.push('Netlist: ' + S.fileName);
  L.push(`Placa: ${r.board.cols} x ${r.board.rows} furos (${r.board.width_mm} x ${r.board.height_mm} mm)`);
  L.push(`Redes roteadas: ${r.stats.nets_routed}  |  incompletas: ${r.stats.nets_failed}`);
  L.push(`Trabalho manual: ${r.stats.operacoes ?? '?'} operacoes `
         + `(${r.stats.corridas ?? '?'} trechos retos, ${r.stats.quinas ?? '?'} dobras, `
         + `${r.stats.vias || 0} vias, ${r.stats.jumpers} jumpers)`);
  const soltos = r.stats.orphan_pins || [];
  if (soltos.length) {
    L.push('');
    L.push('!! ATENCAO: ' + soltos.length + ' PINO(S) SEM LIGACAO - ligue estes a mao:');
    for (const o of soltos) L.push(`   ${o.ref}.${o.pin} no furo ${o.label} (rede ${o.net})`);
  }
  L.push('');
  L.push('COORDENADAS: linha em letra + coluna em numero (A1 = furo superior esquerdo),');
  L.push('vistas pelo LADO DOS COMPONENTES.');
  L.push('');
  L.push('1) COMPONENTES');
  for (const c of b.components) {
    const pins = Object.entries(c.pin_labels).map(([p, lb]) => `${p}:${lb}`).join('  ');
    L.push(`  ${c.ref.padEnd(5)} origem ${c.origin_label} rot ${c.rot}deg  [${c.pattern}]`);
    L.push(`         pinos ${pins}`);
  }
  L.push('');
  const dec = r.decoupling || [];
  if (dec.length) {
    L.push('');
    L.push('1b) DESACOPLAMENTO - montar estes PRIMEIRO e o mais curto possivel');
    for (const d of dec) {
      L.push(`   ${d.cap} junto de ${d.ic} - laco ${d.laco_mm ?? '?'} mm `
             + `(minimo possivel ${d.piso_mm ?? '?'} mm)`);
      L.push(`      ${d.net_pwr}: ${d.pwr_de || '?'} -> ${d.pwr_ate || '?'} (${d.pwr_mm ?? '?'} mm)`);
      L.push(`      ${d.net_gnd}: ${d.gnd_de || '?'} -> ${d.gnd_ate || '?'} (${d.gnd_mm ?? '?'} mm)`);
    }
    L.push('');
  }
  L.push('2) TRILHAS NO LADO DA SOLDA (fio nu / ponte de solda)');
  for (const x of b.bridges) L.push(`  ${x.net.padEnd(18)} ${x.from_label.padEnd(5)} -> ${x.to_label.padEnd(5)}  ${x.holes} furos, ${x.length_mm} mm`);
  if ((b.vias || []).length) {
    L.push('');
    L.push('3) VIAS (pedaco de fio no furo, soldado dos DOIS lados)');
    for (const x of b.vias) L.push(`  ${x.net.padEnd(18)} furo ${x.from_label}`);
  }
  if ((b.top_bridges || []).length) {
    L.push('');
    L.push('4) TRILHAS NO LADO DOS COMPONENTES (fio nu por cima)');
    for (const x of b.top_bridges) L.push(`  ${x.net.padEnd(18)} ${x.from_label.padEnd(5)} -> ${x.to_label.padEnd(5)}  ${x.holes} furos, ${x.length_mm} mm`);
  }
  L.push('');
  L.push('5) JUMPERS (fio isolado sobrevoando)');
  for (const x of b.jumpers) L.push(`  ${x.net.padEnd(18)} ${x.from_label.padEnd(5)} -> ${x.to_label.padEnd(5)}  cortar ${x.cut_mm} mm`);
  L.push('');
  L.push(`TOTAIS: ${b.totals.bridges} trilhas na solda (${b.totals.bridge_mm} mm), ` +
         `${b.totals.top_bridges || 0} por cima (${b.totals.top_bridge_mm || 0} mm), ` +
         `${b.totals.vias || 0} vias, ${b.totals.jumpers} jumpers (${b.totals.wire_cut_mm} mm).`);
  return L.join('\n');
}

function doExport(kind) {
  if (!S.result) return;
  const base = S.fileName.replace(/\.[^.]+$/, '') || 'perfboard';
  if (kind === 'top') download(base + '_componentes.svg', S.result.svg_top, 'image/svg+xml');
  if (kind === 'bottom') download(base + '_solda.svg', S.result.svg_bottom, 'image/svg+xml');
  if (kind === 'json') download(base + '_perfboard.json', JSON.stringify({
    // formato 2: leva a netlist junto, então o arquivo sozinho restaura tudo
    format: 2,
    saved_at: new Date().toISOString(),
    fileName: S.fileName,
    netlist: S.netlist,
    settings: currentSettings(),
    overrides: S.overrides,
    decoupling: S.decoupling,
    board: S.result.board, layout: S.result.layout, routes: S.result.routes,
    stats: S.result.stats, build: S.result.build,
  }, null, 2), 'application/json');
  if (kind === 'txt') download(base + '_montagem.txt', buildText());
}

/* ---------------- eventos ---------------- */

function bind() {
  $('#drop').addEventListener('click', () => $('#file').click());
  $('#file').addEventListener('change', (e) => {
    abrirArquivo(e.target.files[0]);
    e.target.value = '';   // permite reabrir o mesmo arquivo
  });
  ['dragover', 'dragenter'].forEach((ev) =>
    $('#drop').addEventListener(ev, (e) => { e.preventDefault(); $('#drop').classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    $('#drop').addEventListener(ev, (e) => { e.preventDefault(); $('#drop').classList.remove('over'); }));
  $('#drop').addEventListener('drop', (e) => abrirArquivo(e.dataTransfer.files[0]));

  // O "?" mora dentro do <label>: sem isto, clicar nele (ou no texto da dica) abriria
  // o select ao lado, porque o clique no label ativa o campo.
  document.addEventListener('click', (e) => {
    if (e.target.closest('.ajuda')) e.preventDefault();
  });

  // A dica tem largura fixa e o painel e estreito. Ancorada no "?", ela vaza para fora
  // e o scroll do painel corta o texto - entao aqui ela e empurrada para dentro.
  const encaixaDica = (badge) => {
    const d = badge.querySelector('.dica');
    if (!d) return;
    d.style.visibility = 'hidden';
    d.style.display = 'block';          // precisa estar no fluxo para ter tamanho
    const b = badge.getBoundingClientRect();
    const m = 10;
    let x = Math.min(b.left, window.innerWidth - m - d.offsetWidth);
    x = Math.max(x, m);
    // abaixo do "?" se couber; senao acima, para nao sair pela base da tela
    let y = b.bottom + 6;
    if (y + d.offsetHeight > window.innerHeight - m) {
      y = Math.max(m, b.top - 6 - d.offsetHeight);
    }
    d.style.left = Math.round(x) + 'px';
    d.style.top = Math.round(y) + 'px';
    d.style.display = '';
    d.style.visibility = '';
  };
  ['mouseover', 'focusin'].forEach((ev) => document.addEventListener(ev, (e) => {
    const a = e.target.closest && e.target.closest('.ajuda');
    if (a) encaixaDica(a);
  }));

  $('#run').addEventListener('click', () => run(true, false));
  $('#fitBoard').addEventListener('click', () => {
    if (!S.analysis) return;
    $('#cols').value = S.analysis.suggested_board.cols;
    $('#rows').value = S.analysis.suggested_board.rows;
    updateBoardMm();
    run(true, false);
  });

  ['#cols', '#rows', '#margin'].forEach((s) => $(s).addEventListener('input', updateBoardMm));
  $('#scale').addEventListener('change', () => run(false, true));
  $('#labelStyle').addEventListener('change', () => run(false, true));
  $('#faces').addEventListener('change', updateFacesHint);


  $$('[data-export]').forEach((b) => b.addEventListener('click', () => doExport(b.dataset.export)));

  $$('.tab').forEach((t) => t.addEventListener('click', () => {
    $$('.tab').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    $('#views').className = t.dataset.view === 'both' ? '' : 'only-' + t.dataset.view;
  }));

  $('#decoup').addEventListener('change', (e) => {
    const i = e.target.dataset.dec;
    if (i === undefined) return;
    S.decoupling[+i].enabled = e.target.checked;
    run(true, false);
  });

  $('#decoupAdd').addEventListener('click', () => {
    const cap = $('#decoupCap').value, ic = $('#decoupIc').value;
    if (!cap || !ic) return;
    if (S.decoupling.some((d) => d.cap === cap && d.ic === ic)) {
      status(`${cap} já está vinculado a ${ic}`, 'bad');
      return;
    }
    const par = montaPar(cap, ic);
    if (!par) {
      status(`não achei uma alimentação e um terra em comum entre ${cap} e ${ic}`, 'bad');
      return;
    }
    S.decoupling.push(par);
    run(true, false);
  });

  document.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'descartarSessao') {
      e.preventDefault();
      descartaSessao();
      status('sessão guardada descartada — recarregar agora começa do zero', 'ok');
    }
  });

  $('#overlayParar').addEventListener('click', () => {
    // Nao aborta a conexao: pede para o servidor encerrar a busca e devolver o
    // melhor que ja achou. Abortar jogaria fora o trabalho todo.
    S.pedidoPendente = null;
    $('#overlayParar').disabled = true;
    $('#overlayTxt').textContent = 'Encerrando a busca\u2026';
    if (!S.job) {                       // servidor sem canal de parada: aborta mesmo
      if (S.aborto) S.aborto.abort();
      return;
    }
    fetch('api/parar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: S.job }),
    }).catch(() => { if (S.aborto) S.aborto.abort(); });
  });

  $('#complist').addEventListener('click', (e) => {
    const act = e.target.dataset.act;

    if (act === 'size-close') { S.editing = null; renderComponents(); markSelection(); return; }
    if (act === 'size-apply' || act === 'size-reset') {
      const ref = e.target.dataset.ref;
      if (act === 'size-reset') {
        delete S.overrides[ref];
      } else {
        const caixa = document.querySelector(`.sizer[data-ref="${ref}"]`);
        const margens = [0, 0, 0, 0];
        if (caixa) {
          caixa.querySelectorAll('input[data-margin]').forEach((c) => {
            margens[+c.dataset.margin] = Math.max(0, Math.min(30, +c.value || 0));
          });
        }
        S.overrides[ref] = Object.assign({}, S.overrides[ref], { margins: margens });
      }
      S.editing = null;
      run(false, false);
      return;
    }

    const row = e.target.closest('.crow');
    if (!row) return;
    const pl = placementOf(row.dataset.ref);
    S.selected = row.dataset.ref;
    if (act === 'size') {
      S.editing = S.editing === row.dataset.ref ? null : row.dataset.ref;
      renderComponents(); markSelection(); return;
    }
    if (act === 'rot' && pl) { pl.rot = (pl.rot + 90) % 360; run(false, false); return; }
    if (act === 'lock' && pl) { pl.locked = !pl.locked; renderComponents(); markSelection(); return; }
    markSelection();
  });

  document.addEventListener('keydown', (e) => {
    if (!S.selected || /input|select|textarea/i.test(e.target.tagName)) return;
    const pl = placementOf(S.selected);
    if (!pl) return;
    const k = e.key.toLowerCase();
    if (k === 'r') { pl.rot = (pl.rot + 90) % 360; run(false, false); e.preventDefault(); }
    else if (k === 'l') { pl.locked = !pl.locked; renderComponents(); markSelection(); e.preventDefault(); }
    else if (e.key.startsWith('Arrow')) {
      if (pl.locked) return;
      if (e.key === 'ArrowLeft') pl.col--;
      if (e.key === 'ArrowRight') pl.col++;
      if (e.key === 'ArrowUp') pl.row--;
      if (e.key === 'ArrowDown') pl.row++;
      run(false, false);
      e.preventDefault();
    }
  });
}

async function init() {
  bind();
  updateBoardMm();
  updateFacesHint();

  const sessao = leSessao();
  if (sessao) {
    status('restaurando sua sessão anterior…', 'busy');
    try {
      await loadProject(sessao, sessao.fileName || 'sessão');
    } catch (e) {
      status('não consegui restaurar a sessão: ' + e.message, 'bad');
    }
  }
  try {
    // ping so para saber se o servidor esta de pe; se nao estiver, o catch explica
    const res = await fetch('api/examples');
    if (!res.ok) throw new Error('HTTP ' + res.status);
  } catch (e) {
    const comoAbriu = location.protocol === 'file:'
      ? 'A página foi aberta direto do arquivo. Feche e abra <b>http://127.0.0.1:8765</b>.'
      : 'Confira se o servidor está de pé.';
    status('servidor fora do ar', 'bad');
    $('#srcinfo').innerHTML =
      `<div class="msg err"><b>Servidor não respondeu.</b> ${comoAbriu}<br><br>
       Na pasta do projeto, rode:<br><code>python server.py</code></div>`;
  }
}

init();
