# Novidades da Controladoria

O que mudou em cada versão, a mais recente primeiro.

## 1.4.3 — 29/10/2026

- Conectar o Lex: se a Anthropic não aceitar o código, a tela avisa e já abre uma autorização nova. Use só a aba mais recente.
- Depois de colar o código, a tela mostra «Conferindo…» até a resposta e avisa se a Anthropic não responder em 1 minuto.
- Botão «Copiar o link» e uma ajuda para quando a página do Claude disser «Não foi possível verificar seu navegador».
- «Testar acesso» diz exatamente o que houve: sem acesso guardado, acesso recusado ou sem resposta.
- Botão «Copiar diagnóstico»: copia um texto curto, sem senhas nem códigos, para colar no grupo da mentoria.
- A coluna «Chegou do PJe» virou «Chegou»: as publicações vêm do Diário de Justiça Eletrônico (selo «Diário») e as intimações, do PJe (selo «PJe»).
- Atualizar não apaga mais as suas melhorias: antes de trocar o código, a Controladoria guarda o que você mudou em `melhorias-guardadas` e o Claude oferece refazer na versão nova.

## 1.4.2 — 26/10/2026

- Grupos: inclua processos num grupo já criado. Na página do grupo, cole um ou mais números e clique em «Incluir»; a Controladoria confere cada um, não repete o que já está lá e diz quantos entraram e quais ficaram de fora, e por quê.
- Grupos: botão «Retirar» em cada processo do grupo. Os autos já baixados continuam guardados.
- Sem internet na hora da varredura? Em vez de vários avisos técnicos, aparece um só, em português, dizendo o que fazer.
- Computador que acabou de ligar ou de acordar: a varredura espera a internet voltar (até 10 minutos) antes de começar.

## 1.4.1 — 23/10/2026

- O TRF1 saiu da lista de tribunais: o acesso automático do TRF1 depende de liberação oficial do tribunal. Os processos do TRF1 continuam pela pasta do caso: baixe o PDF no PJe, coloque em `autos/` e o Lex começa sozinho.

## 1.4.0 — 22/10/2026

- Nova tela Configurações › Tribunais: guarde o CPF uma vez e a senha de cada tribunal. A senha só é guardada depois que o PJe aceita o login, e cada tribunal mostra um selo (Conectado, Senha recusada, Instável, Fora do ar).
- Se uma senha deixar de funcionar, aparece um aviso no topo do painel, com o caminho para trocá-la.
- TRF1 (1º e 2º grau) entrou na lista de tribunais (retirado na 1.4.1).
- Pasta do caso: para tribunal sem download automático, o cartão mostra «Faltam os autos» e a Controladoria cria a pasta do processo na casa da Banca. Coloque o PDF baixado do PJe em `autos/` e o Lex começa sozinho. O botão «Abrir a pasta» leva direto a ela.
- «Mandar sem os autos»: dá para mandar ao Lex mesmo sem os autos, escrevendo a orientação.
- Os seus arquivos na pasta do caso nunca são apagados nem substituídos: um arquivo do PJe com o mesmo nome entra ao lado, com «(PJe)» no nome.
- Botão de olho nos campos de senha, para ver o que está digitando.

## 1.3.0 — 08/10/2026

- Sem senha do PJe, tudo funciona: guarde só o seu nome e a OAB e as publicações de todos os tribunais já chegam pelo Diário. A senha do PJe ficou opcional e serve para baixar os autos do TJMT e do TJMG.
- Ligar o Lex agora é um botão: em Configurações (⚙), clique em «Conectar o Lex» e autorize na sua conta Claude. Sem PowerShell e sem copiar código.
- Computador desligado no horário da varredura? Ela roda assim que o computador ligar.
- No Windows, a instalação usa o Python oficial (com assinatura digital), que o Controle Inteligente de Aplicativos não bloqueia.
- Os avisos do Lex falam deste computador, não mais do “Mac”, e a chave da ponte sumiu da tela (ela é automática).

## 1.2.1 — 07/10/2026

- Instalação mais paciente: no primeiro início, o Windows pode levar até 3 minutos, e a instalação espera em vez de dar erro.

## 1.2.0 — 07/10/2026

- Leia a íntegra da publicação dentro do painel.
- Diga se o processo é seu: «É meu, acompanhar» ou «Não é meu».
- Veja as partes da publicação quando o processo não tem dados do PJe.
- Adicione qualquer processo à carteira pelo número e passe a acompanhá-lo.

## 1.1.0 — 07/10/2026

- Cadastre o TJMT e o TJMG ao mesmo tempo, cada um com a sua senha.

## 1.0.0 — 07/10/2026

- Primeira versão no seu computador: carteira pelo Diário, quadro de trabalho, autos do PJe (TJMT e TJMG) e ligação com o Lex.
