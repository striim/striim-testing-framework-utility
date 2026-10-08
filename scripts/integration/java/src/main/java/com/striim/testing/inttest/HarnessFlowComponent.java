package com.striim.testing.inttest;

import com.webaction.runtime.components.FlowComponent;

/**
 * The flow component a harness-driven writer is attached to. It exists so the writer's
 * exception-store route ({@code common.ExceptionStoreNotifier}) has a non-null handle and sends;
 * nothing reads it. The base's flush-escalation path calls {@code notifyException}, which the
 * stand-in refuses -- that path catches the refusal and logs it, so attaching a component here
 * changes nothing else.
 */
final class HarnessFlowComponent extends FlowComponent {
}
