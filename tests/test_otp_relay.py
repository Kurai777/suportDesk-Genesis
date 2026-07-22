"""Testes do relay de OTP (ADR-026) — pede o código no grupo e espera a resposta.

Sem rede: WhatsApp é fake; o InboxWhatsApp é o real. A resposta do usuário é simulada
INJETANDO a mensagem no inbox quando o pedido é enviado (como se ele respondesse na hora).
"""

from app.otp_relay import RelayOtp, extrair_otp
from app.whatsapp import InboxWhatsApp, MensagemRecebida

_GRUPO = "120363018941234567@g.us"


def _msg(texto, *, grupo=True, de_mim=False, remetente="5511992568511@s.whatsapp.net", id="r1"):
    return MensagemRecebida(
        chat_jid=_GRUPO if grupo else remetente,
        remetente_jid=remetente,
        remetente_nome="Responsavel",
        texto=texto,
        de_mim=de_mim,
        id=id,
        eh_grupo=grupo,
    )


class FakeWhatsAppReply:
    """Ao enviar o pedido, injeta a resposta no inbox (simula o usuário respondendo)."""

    def __init__(self, inbox, resposta=None):
        self._inbox = inbox
        self._resposta = resposta
        self.enviados = []

    async def enviar(self, destino, texto):
        self.enviados.append((destino, texto))
        if self._resposta is not None:
            self._inbox.registrar(self._resposta)
        return True


def _cfg(settings, **kw):
    base = {"whatsapp_grupo_destino": _GRUPO, "portal_otp_autorizados": []}
    base.update(kw)
    return settings.model_copy(update=base)


# --- extrair_otp (puro) ----------------------------------------------------


def test_extrair_otp_formatos():
    assert extrair_otp("Teste token 656-789") == "656789"
    assert extrair_otp("codigo 656 789") == "656789"
    assert extrair_otp("seu código é 123456") == "123456"
    assert extrair_otp("PIN 4821") == "4821"
    assert extrair_otp("sem numero aqui") is None
    assert extrair_otp("só 12") is None  # curto demais
    assert extrair_otp("") is None


# --- RelayOtp --------------------------------------------------------------


async def test_solicita_posta_no_grupo_e_extrai_o_codigo(settings):
    inbox = InboxWhatsApp()
    wa = FakeWhatsAppReply(inbox, resposta=_msg("o código é 656-789"))
    relay = RelayOtp(wa, inbox, _cfg(settings))

    otp = await relay.solicitar(timeout_s=2, intervalo_s=0.01)

    assert otp == "656789"
    assert wa.enviados and wa.enviados[0][0] == _GRUPO  # pediu NO GRUPO
    assert "código" in wa.enviados[0][1].lower()


async def test_timeout_sem_resposta_devolve_none(settings):
    inbox = InboxWhatsApp()
    wa = FakeWhatsAppReply(inbox, resposta=None)  # ninguém responde
    relay = RelayOtp(wa, inbox, _cfg(settings))

    assert await relay.solicitar(timeout_s=0.05, intervalo_s=0.01) is None
    assert wa.enviados  # ainda assim postou o pedido


async def test_ignora_resposta_fora_do_grupo_e_a_propria(settings):
    inbox = InboxWhatsApp()
    # resposta é DIRETA (não no grupo) -> não conta
    wa = FakeWhatsAppReply(inbox, resposta=_msg("123456", grupo=False))
    relay = RelayOtp(wa, inbox, _cfg(settings))
    assert await relay.solicitar(timeout_s=0.05, intervalo_s=0.01) is None

    inbox2 = InboxWhatsApp()
    # resposta é do próprio bot (de_mim) -> não conta
    wa2 = FakeWhatsAppReply(inbox2, resposta=_msg("123456", de_mim=True))
    relay2 = RelayOtp(wa2, inbox2, _cfg(settings))
    assert await relay2.solicitar(timeout_s=0.05, intervalo_s=0.01) is None


async def test_lista_de_autorizados_filtra_remetente(settings):
    inbox = InboxWhatsApp()
    # resposta de um número NÃO autorizado -> ignorada
    wa = FakeWhatsAppReply(inbox, resposta=_msg("123456", remetente="5511000000000@s.whatsapp.net"))
    relay = RelayOtp(wa, inbox, _cfg(settings, portal_otp_autorizados=["5511992568511"]))
    assert await relay.solicitar(timeout_s=0.05, intervalo_s=0.01) is None


async def test_autorizado_da_lista_passa(settings):
    inbox = InboxWhatsApp()
    wa = FakeWhatsAppReply(inbox, resposta=_msg("654321", remetente="5511992568511@s.whatsapp.net"))
    relay = RelayOtp(wa, inbox, _cfg(settings, portal_otp_autorizados=["5511992568511"]))
    assert await relay.solicitar(timeout_s=2, intervalo_s=0.01) == "654321"


async def test_ignora_mensagem_antiga_do_inbox(settings):
    inbox = InboxWhatsApp()
    inbox.registrar(_msg("999888", id="antiga"))  # já estava lá ANTES do pedido
    wa = FakeWhatsAppReply(inbox, resposta=None)  # ninguém responde depois
    relay = RelayOtp(wa, inbox, _cfg(settings))
    # a mensagem antiga (no snapshot) não deve ser aceita como resposta
    assert await relay.solicitar(timeout_s=0.05, intervalo_s=0.01) is None


async def test_sem_grupo_configurado_nao_pede(settings):
    inbox = InboxWhatsApp()
    wa = FakeWhatsAppReply(inbox, resposta=_msg("123456"))
    relay = RelayOtp(wa, inbox, _cfg(settings, whatsapp_grupo_destino=""))
    assert await relay.solicitar(timeout_s=0.05, intervalo_s=0.01) is None
    assert wa.enviados == []  # nem tentou postar
