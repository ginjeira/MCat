# Ligação futura da conta Telegram e escolha do grupo destino

**Estado:** implementação em curso na branch `feature/telegram-account-destination`; a instância partilhada em 8443 continua a ser a versão principal.

Este documento especifica a ligação interativa de uma conta Telegram ao website do MCat e a escolha de um grupo destino entre os grupos acessíveis a essa conta.

## Experiência pretendida

1. Um utilizador já autenticado no website inicia a ligação Telegram a partir das definições da sua conta.
2. A aplicação inicia o fluxo de autenticação oficial suportado pelo Telegram.
3. A interface pede apenas os dados necessários no momento:
   - identificador da conta, como o número de telefone indicado pelo Telegram;
   - código de início de sessão enviado pelo Telegram;
   - palavra-passe de verificação em dois passos, apenas se a conta a exigir.
4. Depois da autenticação, a aplicação apresenta os grupos que a conta pode usar como destino, com nome e identificadores suficientes para distingui-los.
5. O utilizador escolhe um grupo e confirma a seleção.
6. A aplicação guarda a associação entre o utilizador do website, a sessão Telegram e o grupo escolhido.
7. Os pedidos seguintes desse utilizador encaminham os conteúdos apenas para o grupo confirmado, respeitando permissões, deduplicação e limites existentes.

O website pode solicitar novamente um código ou a palavra-passe de verificação quando o Telegram exigir uma reautenticação. A interface deve explicar por que motivo a ação é necessária e nunca apresentar o segredo armazenado.

## Requisitos de autenticação

- Usar o fluxo interativo do Telegram e validar cada etapa antes de avançar.
- Tratar o código e a palavra-passe de dois passos como segredos temporários.
- Nunca guardar códigos ou palavras-passe em texto simples, registos, métricas, histórico de pedidos ou mensagens de erro.
- Não assumir que todas as contas exigem palavra-passe de dois passos.
- Distinguir estados como ligação pendente, código inválido, palavra-passe inválida, sessão ligada, sessão expirada e ligação revogada.
- Permitir ao utilizador desligar a conta Telegram e revogar a sessão associada.
- Evitar que uma falha ou interrupção deixe a interface a indicar que a conta está ligada quando a autorização não foi confirmada.

## Proteção da sessão

Uma sessão Telegram ativa equivale a uma credencial de acesso à conta. A implementação futura deve:

- armazenar a sessão cifrada em repouso, com acesso restrito ao serviço;
- associá-la ao utilizador correto e impedir que outros utilizadores a leiam ou reutilizem;
- não a expor ao browser, ao código cliente, a exportações ou a suporte técnico;
- limitar permissões de processo, cópias de segurança e retenção;
- remover ou invalidar a sessão ao desligar a conta e definir como tratar sessões expiradas;
- proteger operações de gestão contra pedidos falsificados e acessos entre contas.

A estratégia técnica de armazenamento e cifragem deve ser decidida antes da implementação e documentada juntamente com o modelo de ameaças.

## Escolha do grupo destino

- Listar apenas grupos e canais que a conta autenticada consegue aceder e nos quais o encaminhamento é permitido.
- Pedir confirmação explícita do grupo selecionado; nomes iguais devem ser distinguidos por informação adicional apropriada.
- Guardar a escolha por utilizador, sem substituir a configuração global até a migração para destinos individuais estar definida.
- Revalidar o acesso ao destino quando a conta ou as permissões mudarem.
- Se o destino deixar de estar acessível, suspender os novos encaminhamentos para esse utilizador e apresentar uma ação para escolher outro destino.
- Mostrar o destino escolhido antes da confirmação de cada pedido que possa encaminhar conteúdos.

## Decisões adotadas para esta implementação

- Cada conta web pode ligar uma conta Telegram de cada vez.
- Cada conta Telegram tem um destino predefinido; pode ser alterado nas definições da conta.
- A segunda instância mantém uma porta, cookie, base de dados operacional e sessão independentes da instância partilhada.
- As contas web e as palavras-passe são partilhadas entre as duas instâncias durante a coexistência.
- O código e a palavra-passe de dois passos existem apenas no fluxo temporário de autenticação.
- A sessão é serializada como Telethon StringSession e cifrada com Fernet antes de ser guardada na base de dados. A chave de cifragem é fornecida fora do repositório; a perda da chave invalida a capacidade de abrir sessões existentes.
- Os pedidos validam novamente o acesso ao destino antes de serem aceites. Se o destino já não puder ser usado, o utilizador tem de escolher outro.

## Dependências e decisões anteriores à implementação

- Confirmar os requisitos e limitações do fluxo de autenticação Telegram e da biblioteca utilizada.
- Definir se cada utilizador pode ligar uma ou várias contas Telegram.
- Definir se pode selecionar um destino diferente por pedido ou se existe um destino predefinido por conta.
- Definir a política de retenção e revogação de sessões.
- Rever riscos de privacidade, segurança e permissões antes de expor esta função a utilizadores.

## Critérios de aceitação

- Uma conta Telegram só fica marcada como ligada depois de confirmação do Telegram.
- Código e palavra-passe temporários não aparecem em registos nem persistem após a autenticação.
- A sessão guardada não é acessível a outro utilizador do website.
- A lista de destinos corresponde à conta Telegram ligada.
- O utilizador confirma o grupo antes de o tornar destino dos seus pedidos.
- Revogar ou perder a sessão bloqueia novos encaminhamentos até a ligação ser restaurada.
- Os pedidos nunca recorrem silenciosamente ao grupo destino de outro utilizador.
