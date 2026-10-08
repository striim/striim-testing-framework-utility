package com.webaction.security;

/**
 * Minimal mock of Striim's {@code com.webaction.security.Password} (the real class is abstract with
 * 15+ abstract methods covering vault/encryption resolution). Operators under test only
 * ever {@code instanceof}/{@code checkcast} a {@code Password}-typed property value and
 * call {@code getPlain()} to recover the plaintext, so this harness mock reproduces
 * exactly that surface: a {@code (String)} constructor and a {@code getPlain()} method
 * matching the real one's descriptor {@code ()Ljava/lang/String;}. Not a
 * byte-for-byte reimplementation of the real class's encryption/vault machinery --
 * nothing under test inspects that. Only a plaintext value is supported: a
 * {@code [namespace.vault.key]}-shaped reference would drive an operator's own
 * vault-resolution helper (e.g. a
 * {@code JdbcConnectionFactory.getVaultProperty}) into the real {@code MDCache}/
 * {@code AuthToken}/{@code VaultAPI} platform classes, none of which this harness mocks.
 */
public final class Password {

    private final String plain;

    public Password(String plain) {
        this.plain = plain;
    }

    public String getPlain() {
        return plain;
    }

    @Override
    public String toString() {
        return "Password(****)";
    }
}
