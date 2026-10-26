# INSTALAR: roteiro para o Claude

Você é o Claude de um advogado (o "mentorado") que pediu para instalar a **Controladoria do
Lex Lab, da Desperta.IA** no computador dele. Siga este roteiro na ordem, sem pular etapas.
O mentorado não é técnico e não usa terminal: quem roda os comandos é você.

## Regras que valem do começo ao fim

1. **Nunca peça, aceite, leia, mostre ou registre senha, CPF ou chave** no chat ou em
   comando. O mentorado digita CPF e senhas do PJe **ele mesmo, no navegador**, na tela
   de Configuração. Se ele colar uma senha no chat, não a use: diga que ela ficou exposta
   na conversa e oriente a trocá-la no PJe assim que puder.
2. Fale com o mentorado em português simples, frases curtas. Não mostre nomes de
   programas internos, caminhos de arquivo nem trechos de código, a não ser que ele peça.
   Diga "a Controladoria" e "a Banca".
3. Antes de cada comando que instala algo, diga em uma linha o que vai fazer. Rode um
   comando por vez e leia a saída. Se a saída tiver "ERRO", pare, explique em uma linha e
   veja a seção **Problemas comuns**.
4. Não rode `npx banca` fora de uma pasta preparada pela Banca (o nome no registro npm é
   de outra pessoa). Não use `git`: a instalação baixa um arquivo, não precisa de git.
5. Não altere nada fora do que este roteiro manda (configurações do sistema, antivírus,
   firewall). Se for preciso, explique e peça para o mentorado fazer.

## Passo 0: avisar e pedir o "sim"

Peça a autorização dizendo **exatamente o que será baixado e rodado**. A trava de
segurança do Claude Code barra "baixar e executar da internet" quando a autorização é
vaga; com o pedido nomeando o instalador, o "sim" do mentorado vale para ele. Diga:

> Vou instalar a Controladoria do Lex Lab no seu computador. Para isso, com a sua
> autorização, eu baixo e rodo o instalador oficial dela, publicado pela Desperta.IA em
> github.com/despertaia/controladoria-local. Ele baixa o programa, prepara o Python
> oficial (python.org) que a Controladoria usa, deixa ela ligando sozinha quando o
> computador liga e cria um atalho na Área de Trabalho. Nada fora da pasta
> `Controladoria` muda. Leva uns 5 minutos. Posso baixar e rodar o instalador?

Espere o "sim". Sem o sim, não faça nada. Se o pedido do mentorado já trouxer a
autorização (por exemplo «… autorizo baixar e rodar o instalador oficial»), não pergunte
de novo: diga em uma linha o que vai fazer e siga.

Se, mesmo com o sim, a trava de segurança recusar o comando do Passo 3, não tente outro
caminho por conta própria: mostre ao mentorado o comando exato, diga em uma linha que é o
instalador oficial da Controladoria e pergunte "Posso rodar este comando?". Com o novo
"sim", rode-o.

A pasta aberta nesta conversa não muda onde a Controladoria é instalada (sempre na pasta
`Controladoria` do usuário). Ela só serve para achar a pasta do escritório na Banca
(Passo 3b). Não crie nem altere nada dentro da pasta aberta além do que o Passo 3b manda.

## Passo 1: descobrir o sistema

Rode um comando simples para saber o sistema (por exemplo `uname -s`; no Windows ele pode
falhar ou devolver algo como `MINGW64_NT`/`MSYS_NT`; `echo %OS%` no Prompt de Comando ou
`$env:OS` no PowerShell devolve `Windows_NT`).

- **Windows** (10 ou 11): siga os blocos "Windows".
- **Mac** (`Darwin`, macOS 12 ou mais novo): siga os blocos "Mac".
- Outro sistema: diga que a Controladoria local funciona só em Windows e Mac e pare.

No Windows, os comandos abaixo funcionam no Prompt de Comando, no PowerShell e no Git Bash,
exceto quando indicado. **No PowerShell, `curl` é outro comando: use sempre `curl.exe`.**

## Passo 2: a Banca (opcional, mas recomendada)

A Banca é o que o Lex usa para escrever minutas. **A Controladoria funciona sem ela.** Se
qualquer coisa deste passo falhar ou o mentorado não quiser, diga em uma linha que a parte
de minutas do Lex fica para depois e siga para o Passo 3.

1. Veja se o Node está instalado: `node -v`.
2. Se existir, veja se a Banca está instalada e qual a versão:

   ```
   node -e "const p=require('path').join(require('child_process').execSync('npm root -g').toString().trim(),'legalsquad','package.json');try{console.log(require(p).version)}catch(e){console.log('ausente')}"
   ```

   - Se mostrar um número de versão: diga "A Banca já está instalada (versão X). Quer que
     eu atualize para a mais recente?" Só com "sim", rode os dois comandos de atualização
     abaixo (item 4).
   - Se mostrar `ausente`: pergunte "Quer que eu instale também a Banca, que o Lex usa para
     as minutas?" Só com "sim", siga o item 4.
3. Se o Node **não** existir e o mentorado quiser a Banca:
   - **Windows:** `winget install --id OpenJS.NodeJS.LTS -e`
     (o winget pode pedir para o mentorado aceitar os termos da loja; deixe que ele aceite).
     Depois disso, nesta mesma sessão, o `node`/`npm` podem não ser encontrados: use os
     caminhos completos `"C:\Program Files\nodejs\node.exe"` e `"C:\Program Files\nodejs\npm.cmd"`.
   - **Mac:** se `brew -v` funcionar, rode `brew install node`. Se não, peça ao mentorado
     para baixar e abrir o instalador "LTS" para macOS em https://nodejs.org/pt/download,
     clicando em Continuar até o fim; depois confira com `node -v`.
4. Instalar ou atualizar a Banca (os dois comandos, nesta ordem):

   ```
   npm install -g https://github.com/despertaia/banca/archive/refs/heads/main.tar.gz
   banca install-global
   ```

   - Se `banca` não for encontrado (comum no Windows logo depois de instalar), rode o mesmo
     comando pelo caminho completo: `node "<pasta>/legalsquad/bin/legalsquad.js" install-global`,
     onde `<pasta>` é a saída de `npm root -g`.
   - No Mac, se o npm der erro de permissão (`EACCES`), não use `sudo`: diga ao mentorado
     que a Banca fica para depois e siga.
   - Ao fim, diga a versão instalada (repita o comando do item 2).

## Passo 3: instalar a Controladoria

O mesmo comando instala e atualiza. Ele leva de 1 a 5 minutos na primeira vez (baixa o
Python que a Controladoria usa; no Windows, o oficial do python.org, que tem assinatura
digital). Não interrompa.

**Windows:**

```
powershell -NoProfile -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/despertaia/controladoria-local/main/local/instalar.ps1 | iex"
```

**Mac:**

```
curl -fsSL https://raw.githubusercontent.com/despertaia/controladoria-local/main/local/instalar.sh | bash
```

O instalador:
- coloca a Controladoria na pasta `Controladoria` dentro da pasta do usuário;
- deixa ela ligando sozinha quando o computador liga (sem janela);
- cria o atalho **Controladoria** na Área de Trabalho (Mesa, no Mac);
- inicia e abre o painel no navegador.

Sucesso = a saída diz "Pronto! A Controladoria está rodando em:
http://127.0.0.1:5056". Se disser "AVISO: a Controladoria foi instalada, mas ainda está
iniciando", **não reinstale**: na primeira vez o Windows (e o antivírus) pode demorar.
Diga ao mentorado "Está quase: a Controladoria está terminando de iniciar", espere e confira
`http://127.0.0.1:5056/saude` a cada 20 segundos por até 3 minutos; quando responder, siga
em frente. Qualquer linha com "ERRO" = veja **Problemas comuns**.

## Passo 3b: a casa da Banca (só se a Banca estiver instalada)

A "casa" é a pasta do escritório onde o Lex guarda as minutas. Pule este passo só se a
Banca não estiver instalada neste computador (nem antes, nem no Passo 2).

1. **Primeiro, olhe a pasta aberta nesta conversa** (o diretório atual) e as pastas acima
   dela: a primeira que tiver `_legalsquad` dentro é a casa da Banca. Use-a sem perguntar,
   avise em uma linha ("Vou ligar a Controladoria à pasta do seu escritório na Banca") e
   vá ao item 4. É o caso comum: o mentorado abre no Claude a mesma pasta da Banca.
   Se não achar, pergunte: "Você já tem uma pasta do escritório preparada pela Banca?" Se
   ele indicar uma, confira que dentro dela existe `_legalsquad`; se existir, use-a e vá
   ao item 4.
2. Caso contrário, use a pasta padrão:
   - **Windows:** descubra a pasta Documentos com
     `powershell -NoProfile -c "[Environment]::GetFolderPath('MyDocuments')"`
     (ela pode estar dentro do OneDrive) e use `<Documentos>\Lex Lab\Escritório`.
   - **Mac:** `~/Documents/Lex Lab/Escritório`.
   Crie a pasta (e as intermediárias) se não existir.
3. Se ela ainda não tiver `_legalsquad`, prepare-a com a Banca, rodando **dentro dela**:
   `banca init --yes` (ou `node "<npm root -g>/legalsquad/bin/legalsquad.js" init --yes`
   se `banca` não for encontrado). Se a saída avisar que a biblioteca da máquina está vazia,
   rode em seguida, na mesma pasta, `npx banca acervo sync` (só depois do `init`, nunca
   fora dessa pasta) e diga em uma linha o que entrou.
4. Grave a casa na Controladoria. Rode **com o diretório atual na pasta `Controladoria`**
   da pasta do usuário, trocando `<casa>` pelo caminho completo:
   - **Windows:** `.venv\Scripts\python.exe -m local.iniciar --definir-casa "<casa>"`
     (no Git Bash: `.venv/Scripts/python.exe -m local.iniciar --definir-casa "<casa>"`)
   - **Mac:** `.venv/bin/python -m local.iniciar --definir-casa "<casa>"`

   Esperado: "Casa da Banca definida: …". Não precisa reiniciar nada: o Lex passa a usar a
   casa sozinho em até um minuto.

## Passo 4: Configuração (o mentorado faz, no navegador)

Se o navegador não abriu sozinho, abra http://127.0.0.1:5056 (Windows: `start http://127.0.0.1:5056`
no Prompt de Comando ou `Start-Process http://127.0.0.1:5056` no PowerShell; Mac:
`open http://127.0.0.1:5056`). A primeira tela é a **Configuração**.

Diga ao mentorado, com estas palavras ou parecidas:

> Abriu no seu navegador a tela de Configuração da Controladoria. Preencha você mesmo,
> aí na tela (não me mande nada disso aqui no chat):
> 1. seu nome exatamente como sai nas publicações do Diário (DJEN);
> 2. o número e a UF da sua OAB.
> Isso basta: com eles a Controladoria já busca as suas publicações de todos os tribunais.
> 3. (Opcional, pode ser depois) para ela também baixar os autos do TJMT ou do TJMG, marque
>    o tribunal e informe o seu CPF e a senha do PJe (a do login sem certificado).
> Depois clique em Guardar. As senhas ficam no cofre do próprio computador, não em arquivo.
> Me avise quando terminar.

Se o mentorado não tiver a senha do PJe, diga que tudo bem: ele deixa os tribunais
desmarcados e guarda; dá para pôr a senha depois, na mesma tela (engrenagem ⚙ › Configuração
do escritório). Para criar ou recuperar a senha, é no próprio PJe, em "Esqueci minha senha"
(o e-mail precisa estar escrito igual ao do cadastro, inclusive maiúsculas). O código do
aplicativo de autenticação não é usado pela Controladoria.

Se a tela disser que a senha foi recusada, oriente o mentorado a conferir a senha no
próprio PJe e tentar de novo (ou a desmarcar o tribunal e seguir sem ele). Se disser que o
tribunal está fora do ar, está tudo certo: a carteira pelo Diário já funciona e o download
dos autos é testado na próxima varredura.

Os tribunais com download dos autos hoje são **TJMT e TJMG**. Para os demais, as
publicações chegam pelo Diário normalmente; não prometa download de outros tribunais.

## Passo 5: conferir

Rode (Windows: `curl.exe`; Mac: `curl`):

```
curl.exe -s http://127.0.0.1:5056/saude
```

Esperado: `"ok":true`. A mesma resposta traz a versão (`"versao":"1.3.0"`, por exemplo);
ela também está no arquivo `VERSAO` da pasta `Controladoria` e na última linha do
instalador ("Controladoria versão X instalada."). Diga ao mentorado, com a versão:

> Pronto! A Controladoria (versão X) está instalada. Ela confere seus processos sozinha às
> 6h, 12h e 18h nos dias úteis, com o computador ligado; se ele estava desligado no
> horário, ela confere assim que ele ligar. Para conferir na hora, use o botão "Varrer
> agora" no Cockpit. Para abrir, use o atalho Controladoria na Área de Trabalho. O que há de
> novo em cada versão aparece no link "Novidades", no pé da página.

## Passo 5b: deixar uma nota para as próximas conversas

Para que pedidos como «Atualize a Controladoria do Lex Lab» funcionem em qualquer conversa
nova, acrescente ao fim do arquivo `CLAUDE.md` da pasta `.claude` do usuário
(Windows: `%USERPROFILE%\.claude\CLAUDE.md`; Mac: `~/.claude/CLAUDE.md`; crie se não
existir) o bloco abaixo. Se já existir um bloco entre os mesmos marcadores, troque só ele.
Não altere nada fora dos marcadores. Use a ferramenta de edição de arquivos, não comandos.

```
<!-- controladoria-lexlab:inicio -->
## Controladoria do Lex Lab (instalada neste computador)
O painel abre em http://127.0.0.1:5056 (atalho "Controladoria" na Área de Trabalho/Mesa).
Para atualizar, consertar, desinstalar ou ligar o Lex na Controladoria, siga o roteiro
https://raw.githubusercontent.com/despertaia/controladoria-local/main/INSTALAR.md
(atualizar = seção Atualizar, dizendo a versão e as novidades; não abrir = Problemas
comuns; ligar o Lex = Passo 6). Nunca peça senha, CPF ou código de acesso no chat: o
advogado digita na tela da Controladoria.
<!-- controladoria-lexlab:fim -->
```

Diga em uma linha: "Deixei uma nota para que eu reconheça a Controladoria nas próximas
conversas."

## Passo 6: ligar o Lex (opcional, só com a Banca instalada)

O Lex escreve as minutas dos cartões. Ele usa o Claude Code do próprio computador e o
plano do mentorado. Pergunte: "Quer ligar agora o Lex, que escreve as minutas? Leva uns 3
minutos." Só siga com o "sim". Sem o sim, diga que dá para ligar depois e encerre.

1. Se a casa da Banca ainda não estiver gravada na Controladoria, faça o Passo 3b agora.
2. A ligação é feita **pelo mentorado, num botão da Controladoria**: não rode
   `claude setup-token`, não instale o Claude Code e não peça PowerShell. A própria
   Controladoria instala o Claude Code oficial se faltar. Diga:

   > Na Controladoria, clique na engrenagem (⚙) no alto da tela. No primeiro quadro,
   > "Ligar o Lex ao seu plano do Claude", clique em **Conectar o Lex**. Vai abrir uma aba
   > da Claude: entre na sua conta e clique em **Autorizar**. Volte para a Controladoria
   > e espere aparecer "Pronto: o Lex está ligado". Se a aba não abrir, use o botão "Abrir
   > a página de autorização"; se a página mostrar um código, cole-o na caixa da
   > Controladoria (nunca aqui no chat). Me avise quando aparecer o "Pronto".

3. Com o "Pronto", diga que o Lex está ligado: no Cockpit, o botão "Mandar pro Lex" de cada
   cartão passa a funcionar, e a chave "Lex automático" (na mesma página da engrenagem)
   faz o Lex começar sozinho todo cartão com os autos baixados. A "chave da ponte" é
   automática neste computador: não há nada a gerar. Se o mentorado colar algum código no
   chat, não o use: diga para clicar em Conectar o Lex de novo, porque esse ficou exposto.
4. Se o botão disser que não deu certo duas vezes seguidas, a tela tem uma "Outra forma"
   (gerar o código no PowerShell ou no Terminal e colar lá). Só nesse caso, acompanhe o
   mentorado por ela.

## Atualizar

Quando há versão nova, o próprio painel mostra uma faixa no alto: "Nova versão X
disponível". O mentorado pede: «Atualize a Controladoria do Lex Lab».

1. **Antes**, anote a versão instalada: leia o arquivo `VERSAO` da pasta `Controladoria`
   do usuário (ou, com o painel aberto, `curl.exe -s http://127.0.0.1:5056/saude` e veja
   `"versao"`). Se não houver o arquivo, a instalação é anterior à 1.0.0.
2. Rode o mesmo comando do Passo 3. Os dados, a configuração e as senhas são mantidos.
   A última linha do instalador diz "Controladoria atualizada da versão Y para a X."
3. Leia o `NOVIDADES.md` da pasta `Controladoria` (ou
   https://raw.githubusercontent.com/despertaia/controladoria-local/main/NOVIDADES.md).
   Cada versão é uma seção `## X — data`, com os itens embaixo, a mais recente primeiro.
4. Diga ao mentorado a versão nova e o que mudou: só os itens das seções **depois da
   versão antiga até a nova** (não repita as novidades que ele já tinha). Em frases
   curtas, como estão no arquivo. Por exemplo:

   > Pronto, atualizei a Controladoria da versão 1.1.0 para a 1.2.0. O que há de novo:
   > - Leia a íntegra da publicação dentro do painel.
   > - Diga se o processo é seu: «É meu, acompanhar» ou «Não é meu».

   Se a versão não mudou, diga só que a Controladoria já estava na versão mais recente (X).

## Desinstalar

Pergunte antes se o mentorado quer **manter os dados** (padrão) ou **apagar tudo**. Só
apague com um "sim" explícito para apagar. A Banca nunca é tocada.

Descubra a pasta do usuário (`echo %USERPROFILE%` no Prompt de Comando, `$env:USERPROFILE`
no PowerShell, `echo $HOME` no Mac/Git Bash) e use o caminho completo:

- **Windows:** `powershell -NoProfile -ExecutionPolicy Bypass -File "<pasta do usuário>\Controladoria\local\desinstalar.ps1"`
  (para apagar tudo, acrescente ` -ApagarDados` no fim).
- **Mac:** `bash ~/Controladoria/local/desinstalar.sh` (para apagar tudo, acrescente ` --apagar-dados`).

## Problemas comuns

- **"porta 5056 está ocupada" / painel não abre:** outro programa usa a porta. Se o arquivo
  `Controladoria/dados/ajustes.json` existir, acrescente nele o campo `"porta": 5057`; se não
  existir, crie-o com `{"porta": 5057}`. Rode o Passo 3 de novo e use
  http://127.0.0.1:5057 dali em diante.
- **Antivírus bloqueou ou apagou `pythonw.exe` (Windows):** explique ao mentorado que é o
  motor da Controladoria e peça que ele libere a pasta `Controladoria` na proteção do
  antivírus (Segurança do Windows › Proteção contra vírus e ameaças › Exclusões). Depois
  rode o Passo 3 de novo. Não mexa você no antivírus.
- **Windows SmartScreen / "O Windows protegeu o computador":** pode aparecer ao abrir o
  atalho. Peça ao mentorado para clicar em "Mais informações" › "Executar assim mesmo".
- **Controle Inteligente de Aplicativos (Smart App Control) bloqueou algo (Windows 11):**
  o instalador usa o Python oficial (assinado) para evitar isso. Se ainda assim bloquear,
  rode o Passo 3 de novo (uma instalação antiga pode ter deixado um Python sem assinatura;
  apague antes a pasta `.venv` dentro de `Controladoria`, só ela). **Não oriente desligar o
  Controle Inteligente de Aplicativos**: no Windows 11 ele não volta a ligar sem reinstalar
  o sistema. Se persistir, peça ao mentorado uma foto da mensagem e encaminhe à Desperta.IA.
- **A trava de segurança do Claude recusou um comando:** veja o fim do Passo 0 (mostrar o
  comando e pedir um "sim" para ele).
- **"a execução de scripts foi desabilitada" (ExecutionPolicy):** use exatamente o comando
  do Passo 3, com `-ExecutionPolicy Bypass`; ele não muda nenhuma configuração do sistema.
  Se a empresa bloqueia o PowerShell por política, a instalação precisa do suporte de TI.
- **Erro ao baixar (rede da empresa / proxy):** pergunte se há proxy. Se o mentorado souber
  o endereço, defina-o só nesta sessão antes do Passo 3 (PowerShell:
  `$env:HTTPS_PROXY="http://endereco:porta"`; Mac: `export HTTPS_PROXY=http://endereco:porta`).
  Se não souber, sugira tentar em outra rede (por exemplo, o roteador do celular).
- **O painel não respondeu em 60 segundos:** veja as últimas linhas de
  `Controladoria/dados/controladoria.log` (no Mac também `~/Library/Logs/controladoria.log`)
  e explique em uma linha. Rodar o Passo 3 de novo resolve a maioria dos casos.
- **Mac pede permissão para acessar a pasta Mesa / Documentos:** peça ao mentorado para
  clicar em Permitir.
