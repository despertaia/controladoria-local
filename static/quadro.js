// Quadro vivo do Cockpit: atualiza sozinho a cada 20 s (só quando algo mudou),
// desliza os cartões que trocam de coluna e envia os botões sem recarregar.
(function () {
  var raiz = document.getElementById('quadro');
  if (!raiz) return;
  var meta = document.querySelector('meta[name="csrf"]');
  var csrf = meta ? meta.content : '';
  var calmo = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var aviso = document.getElementById('quadro-aviso');
  var timerAviso = null;
  var focoNaChave = false;  // a chave "Lex automático" foi acionada: devolve o foco a ela após a troca

  function posicoes() {
    var mapa = {};
    raiz.querySelectorAll('[data-cartao]').forEach(function (el) {
      mapa[el.dataset.cartao] = el.getBoundingClientRect();
    });
    return mapa;
  }

  // Mantém o número ao lado de "Cockpit" no menu igual ao do quadro.
  function atualizarContador() {
    var cab = raiz.querySelector('[data-na-triagem]');
    if (!cab) return;
    var n = parseInt(cab.dataset.naTriagem, 10) || 0;
    var selo = document.querySelector('.navegacao .contador-nav');
    if (!selo) {
      var link = document.querySelector('.navegacao a');
      if (!link || !n) return;
      link.appendChild(document.createTextNode(' '));
      selo = document.createElement('span');
      selo.className = 'contador contador-nav';
      link.appendChild(selo);
    }
    selo.hidden = !n;
    selo.textContent = n;
    selo.setAttribute('aria-label', n + ' na triagem');
  }

  // "Mandar pro Lex" aberto (e o que já foi digitado) sobrevive à troca do quadro.
  function abertos() {
    var mapa = {};
    raiz.querySelectorAll('details.mandar-lex[open]').forEach(function (d) {
      var cartao = d.closest('[data-cartao]');
      if (!cartao) return;
      var campo = d.querySelector('textarea');
      mapa[cartao.dataset.cartao] = {
        texto: campo ? campo.value : '',
        foco: !!campo && document.activeElement === campo,
        fim: campo ? campo.selectionEnd : 0
      };
    });
    return mapa;
  }

  function reabrir(mapa) {
    Object.keys(mapa).forEach(function (id) {
      var cartao = raiz.querySelector('[data-cartao="' + id + '"]');
      var d = cartao && cartao.querySelector('details.mandar-lex');
      if (!d) return;
      d.open = true;
      var campo = d.querySelector('textarea');
      if (!campo) return;
      campo.value = mapa[id].texto;
      if (mapa[id].foco) {
        campo.focus({ preventScroll: true });
        campo.setSelectionRange(mapa[id].fim, mapa[id].fim);
      }
    });
  }

  // Sombra na borda direita enquanto houver colunas escondidas à direita.
  function marcarRolagem() {
    var faixa = raiz.querySelector('.quadro-colunas');
    var moldura = raiz.querySelector('.quadro-faixa');
    if (!faixa || !moldura) return;
    var resta = faixa.scrollWidth - faixa.clientWidth - faixa.scrollLeft;
    moldura.classList.toggle('tem-mais', resta > 2);
  }
  raiz.addEventListener('scroll', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('quadro-colunas')) marcarRolagem();
  }, true);
  window.addEventListener('resize', marcarRolagem);

  function trocar(html, versao) {
    var antes = posicoes();
    var abertosAntes = abertos();
    var chaveAntes = document.activeElement;
    if (chaveAntes && chaveAntes.classList && chaveAntes.classList.contains('chave')) focoNaChave = true;
    var faixa = raiz.querySelector('.quadro-colunas');
    var rolagem = faixa ? faixa.scrollLeft : 0;
    raiz.innerHTML = html;
    reabrir(abertosAntes);
    if (focoNaChave) {
      var chave = raiz.querySelector('.chave');
      if (chave) chave.focus({ preventScroll: true });
      focoNaChave = false;
    }
    if (versao) raiz.dataset.versao = versao;
    faixa = raiz.querySelector('.quadro-colunas');
    if (faixa && rolagem) faixa.scrollLeft = rolagem;
    marcarRolagem();
    atualizarContador();
    if (calmo) return;
    raiz.querySelectorAll('[data-cartao]').forEach(function (el) {
      var a = antes[el.dataset.cartao];
      if (!a) { el.classList.add('cartao-novo'); return; }
      var d = el.getBoundingClientRect();
      var dx = a.left - d.left, dy = a.top - d.top;
      if (!dx && !dy) return;
      el.style.transition = 'none';
      el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          el.style.transition = 'transform 450ms cubic-bezier(.2,.7,.2,1)';
          el.style.transform = '';
        });
      });
    });
  }

  function recarregar() {
    window.location.reload();
  }

  function atualizar(forcar) {
    var url = raiz.dataset.url + (forcar ? '' : '?versao=' + encodeURIComponent(raiz.dataset.versao || ''));
    return fetch(url, { headers: { 'Accept': 'text/html' }, credentials: 'same-origin' })
      .then(function (r) {
        // Sessão vencida: o servidor manda para o login. Não injeta a página de login no quadro.
        if (r.redirected) { recarregar(); return; }
        if (r.status !== 200) { if (r.status !== 204) focoNaChave = false; return; }
        var versao = r.headers.get('X-Versao');
        if (!versao) { recarregar(); return; }
        return r.text().then(function (html) { trocar(html, versao); });
      })
      // Atualização falhou: o foco não fica "prometido" para uma troca que não veio.
      .catch(function () { focoNaChave = false; });
  }

  function mostrar(texto, desfazer) {
    if (!aviso) return;
    clearTimeout(timerAviso);
    aviso.textContent = texto + ' ';
    if (desfazer) {
      var botao = document.createElement('button');
      botao.type = 'button';
      botao.className = 'btn btn-pequeno btn-secundario';
      botao.textContent = 'Desfazer';
      botao.addEventListener('click', function () {
        aviso.hidden = true;
        desfazer();
      });
      aviso.appendChild(botao);
    }
    aviso.hidden = false;
    timerAviso = setTimeout(function () { aviso.hidden = true; }, 8000);
  }

  function postar(url, dados) {
    return fetch(url, {
      method: 'POST', body: dados, credentials: 'same-origin',
      headers: { 'X-CSRF': csrf, 'Accept': 'application/json' }
    }).then(function (r) {
      var tipo = r.headers.get('Content-Type') || '';
      // Redirecionou (sessão vencida) ou não veio JSON: recarrega a página.
      if (r.redirected || tipo.indexOf('json') === -1) {
        recarregar();
        return new Promise(function () {});
      }
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    });
  }

  raiz.addEventListener('submit', function (e) {
    var form = e.target.closest('form.quadro-acao');
    if (!form) return;
    e.preventDefault();
    var botao = form.querySelector('button');
    if (form.classList.contains('quadro-chave')) focoNaChave = true;
    if (botao) botao.disabled = true;
    var destino = form.action;
    postar(destino, new FormData(form)).then(function (res) {
      if (!res.ok) {
        focoNaChave = false;
        if (botao) botao.disabled = false;
        mostrar(res.j.erro || 'Não foi possível concluir.');
      } else {
        var volta = res.j.desfazer;
        mostrar(res.j.mensagem || 'Feito.', volta ? function () {
          var dados = new FormData();
          dados.append('para', volta);
          postar(destino, dados).then(function (r2) {
            if (!r2.ok) mostrar(r2.j.erro || 'Não deu para desfazer.');
            atualizar(true);
          }).catch(function () {
            mostrar('Sem conexão com o painel. Tente de novo.');
          });
        } : null);
      }
      return atualizar(true);
    }).catch(function () {
      mostrar('Sem conexão com o painel. Tente de novo.');
      focoNaChave = false;
      if (botao) botao.disabled = false;
    });
  });

  marcarRolagem();
  setInterval(function () { if (!document.hidden) atualizar(false); }, 20000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) atualizar(false);
  });
})();
