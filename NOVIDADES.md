# Novidades da Controladoria

O que mudou em cada versão, a mais recente primeiro.

## 1.6.0 — 04/11/2026

- Aba Prazos: lista (Atrasados, Hoje, Esta semana, Depois, A conferir, A definir), semana e mês. O «A conferir» se divide em próximos e vencidos, e o vencido nunca esconde o que vence hoje. O contador do menu mostra os que vencem em até 2 dias úteis, os atrasados e os de data ilegível.
- O Lex lê a publicação e o sistema conta, com o cálculo dia a dia na tela. É uma sugestão: o prazo só vale depois de você clicar em «Conferir» ou corrigir. Se o prazo mudou depois que você abriu a tela, a conferência é recusada e o aviso pede que você veja a data nova. A leitura não tem hora marcada: se preferir, preencha à mão.
- Na dúvida, o sistema mostra a data mais cedo ou «prazo a definir», e diz por quê. Prazo em horas ou meses, direito material, rito eleitoral, tribunais superiores e início fora do diário ficam para você preencher.
- Data de trabalho: D-0, D-1, D-2 ou D-3 em um clique, ou outro dia. O padrão do escritório fica em Configurações › Prazos.
- Prazo em dobro nunca é automático: se houver indício, o sistema avisa, e você liga com um clique.
- Quando o prazo passa pelo fim de ano, a data sem a suspensão de fim de ano aparece ao lado, com o aviso das exceções (réu preso, Maria da Penha, urgência, alimentos). Confira antes de agir.
- Intimação do PJe: o sistema nunca abre o teor. Mostra «abra no PJe» e o dia em que a ciência acontece sozinha. Ela é comparada com as publicações do mesmo processo, no mesmo cartão ou em outro, quando chegou até 15 dias antes ou depois. Vale a data que vence primeiro, com a outra ao lado, e você pode escolher a maior. Se a ciência já foi registrada no PJe e o dia não é conhecido, a conta parte do envio até você informar a data.
- Selo no cartão e faixa em todas as páginas, com um número por grupo da aba. Marque «Cumprido» no prazo. Levar o cartão a «Protocolado» marca como cumpridos os prazos que já têm data, com «Desfazer»; o que ainda não tem data continua aberto.
- Arquivar não apaga o prazo: a confirmação avisa o próximo prazo, os atrasados e os sem data, e todos continuam na aba Prazos.
- Calendário forense em Configurações › Prazos: feriados nacionais, fim de ano e os calendários de 2026 de TJMT, TJMG, TRF1 e TRF6, cada um com a fonte. Os dias seguidos iguais, como o recesso, aparecem numa linha só, «de tal dia a tal dia», e cada dia continua com os seus botões. O feriado municipal é seu: cadastre os da sua comarca. Sem calendário do ano, o sistema avisa.
- Agenda: assine em Configurações › Prazos. Cada evento traz só o número do processo e o ato, sem nome de cliente. O que você ainda não conferiu sai marcado «[A CONFERIR]», e os prazos conferidos cuja data fatal passou há mais de um ano saem do arquivo. O lembrete pode não tocar em calendário assinado no Google. Na versão instalada no seu computador, o celular não alcança o endereço: use «Baixar agenda» e importe o arquivo, que não se atualiza sozinho.
- Peça que o Lex ainda não sabe fazer: ele monta um time para ela, da mesma área do caso, e escreve a peça até o fim, com a conferência de citações e a sua revisão de sempre.

## 1.5.0 — 01/11/2026

- Arquivar: tire da carteira ativa um processo que você não quer mais acompanhar. Os cartões sem peça pronta saem do quadro e a Controladoria para de consultá-lo no PJe e de baixar os autos. Se sair publicação nova, ele volta sozinho, com aviso.
- Excluir: o processo vai para a lixeira por 30 dias, com «Restaurar». Os autos e a pasta do caso nunca são apagados. Se sair publicação nova, ele volta para a carteira (em «A conferir», se a sua OAB não consta nele).
- Carteira com as abas «Arquivados» (selo «pelo tribunal» ou «por você») e «Lixeira» («faltam N dias»). «Desarquivar» vale também para o arquivado pelo tribunal.
- Caixas de seleção na carteira: arquive, exclua, desarquive ou restaure até 200 processos de uma vez, com confirmação.
- No menu «⋯» do cartão: «Arquivar processo» e «Excluir processo»; na página do processo, também «Desarquivar» e «Restaurar». Com o Lex trabalhando no processo, espere ele terminar ou desista do pedido.
- No Cockpit, uma faixa avisa quantos processos voltaram por publicação nova; na carteira e na página do processo, o selo «Voltou: publicação de dd/mm».
- A planilha exportada ganhou a aba «Arquivados e lixeira», com o estado na coluna Situação.

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
