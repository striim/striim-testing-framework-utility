package com.example.common;

/**
 * The credential seam: resolves a Striim {@code Password} property's decrypted plain value into the
 * secret a connector should actually use. The one production implementation is
 * {@link VaultSecretResolver}, platform-vault-backed; tests inject a trivial double, and since the
 * interface is functional a lambda like {@code value -> value} suffices.
 *
 * <p>Mirrors the {@link TypeResolver}/{@link MdrTypeResolver} interface-plus-impl pattern: platform
 * coupling — the vault, the security-manager token — lives only in the production impl, so a
 * {@code Processor} core or collaborator taking this interface stays hermetically unit-testable.
 * Deliberately resolve-a-secret only: no {@code AuthToken} or other platform type appears on this
 * surface.</p>
 */
@FunctionalInterface
public interface SecretResolver {

    /**
     * Resolves {@code value}, the decrypted plain value of a {@code Password} property:
     *
     * <ul>
     * <li>plain text (no {@code [...]} brackets) passes through verbatim;</li>
     * <li>a {@code [namespace.vaultName.key]} vault reference resolves to the secret stored in the
     * platform vault;</li>
     * <li>a malformed reference or a failed vault lookup logs the cause and returns {@code null} —
     * the caller proceeds without a value, typically failing later at connection time, with the
     * vault cause already on record.</li>
     * </ul>
     */
    /** Identity resolver that passes through values unchanged. Useful for testing and defaults. */
    SecretResolver IDENTITY = value -> value;

    String resolve(String value);
}
