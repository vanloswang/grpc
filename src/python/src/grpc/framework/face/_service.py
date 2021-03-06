# Copyright 2015, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Behaviors for servicing RPCs."""

# base_interfaces and interfaces are referenced from specification in this
# module.
from grpc.framework.base import interfaces as base_interfaces  # pylint: disable=unused-import
from grpc.framework.face import _control
from grpc.framework.face import exceptions
from grpc.framework.face import interfaces  # pylint: disable=unused-import
from grpc.framework.foundation import abandonment
from grpc.framework.foundation import callable_util
from grpc.framework.foundation import stream
from grpc.framework.foundation import stream_util


class _ValueInStreamOutConsumer(stream.Consumer):
  """A stream.Consumer that maps inputs one-to-many onto outputs."""

  def __init__(self, behavior, context, downstream):
    """Constructor.

    Args:
      behavior: A callable that takes a single value and an
        interfaces.RpcContext and returns a generator of arbitrarily many
        values.
      context: An interfaces.RpcContext.
      downstream: A stream.Consumer to which to pass the values generated by the
        given behavior.
    """
    self._behavior = behavior
    self._context = context
    self._downstream = downstream

  def consume(self, value):
    _control.pipe_iterator_to_consumer(
        self._behavior(value, self._context), self._downstream,
        self._context.is_active, False)

  def terminate(self):
    self._downstream.terminate()

  def consume_and_terminate(self, value):
    _control.pipe_iterator_to_consumer(
        self._behavior(value, self._context), self._downstream,
        self._context.is_active, True)


def _pool_wrap(behavior, operation_context):
  """Wraps an operation-related behavior so that it may be called in a pool.

  Args:
    behavior: A callable related to carrying out an operation.
    operation_context: A base_interfaces.OperationContext for the operation.

  Returns:
    A callable that when called carries out the behavior of the given callable
      and handles whatever exceptions it raises appropriately.
  """
  def translation(*args):
    try:
      behavior(*args)
    except (
        abandonment.Abandoned,
        exceptions.ExpirationError,
        exceptions.CancellationError,
        exceptions.ServicedError,
        exceptions.NetworkError) as e:
      if operation_context.is_active():
        operation_context.fail(e)
    except Exception as e:
      operation_context.fail(e)
  return callable_util.with_exceptions_logged(
      translation, _control.INTERNAL_ERROR_LOG_MESSAGE)


def adapt_inline_value_in_value_out(method):
  def adaptation(response_consumer, operation_context):
    rpc_context = _control.RpcContext(operation_context)
    return stream_util.TransformingConsumer(
        lambda request: method.service(request, rpc_context), response_consumer)
  return adaptation


def adapt_inline_value_in_stream_out(method):
  def adaptation(response_consumer, operation_context):
    rpc_context = _control.RpcContext(operation_context)
    return _ValueInStreamOutConsumer(
        method.service, rpc_context, response_consumer)
  return adaptation


def adapt_inline_stream_in_value_out(method, pool):
  def adaptation(response_consumer, operation_context):
    rendezvous = _control.Rendezvous()
    operation_context.add_termination_callback(rendezvous.set_outcome)
    def in_pool_thread():
      response_consumer.consume_and_terminate(
          method.service(rendezvous, _control.RpcContext(operation_context)))
    pool.submit(_pool_wrap(in_pool_thread, operation_context))
    return rendezvous
  return adaptation


def adapt_inline_stream_in_stream_out(method, pool):
  """Adapts an interfaces.InlineStreamInStreamOutMethod for use with Consumers.

   RPCs may be serviced by calling the return value of this function, passing
   request values to the stream.Consumer returned from that call, and receiving
   response values from the stream.Consumer passed to that call.

  Args:
    method: An interfaces.InlineStreamInStreamOutMethod.
    pool: A thread pool.

  Returns:
    A callable that takes a stream.Consumer and a
      base_interfaces.OperationContext and returns a stream.Consumer.
  """
  def adaptation(response_consumer, operation_context):
    rendezvous = _control.Rendezvous()
    operation_context.add_termination_callback(rendezvous.set_outcome)
    def in_pool_thread():
      _control.pipe_iterator_to_consumer(
          method.service(rendezvous, _control.RpcContext(operation_context)),
          response_consumer, operation_context.is_active, True)
    pool.submit(_pool_wrap(in_pool_thread, operation_context))
    return rendezvous
  return adaptation


def adapt_event_value_in_value_out(method):
  def adaptation(response_consumer, operation_context):
    def on_payload(payload):
      method.service(
          payload, response_consumer.consume_and_terminate,
          _control.RpcContext(operation_context))
    return _control.UnaryConsumer(on_payload)
  return adaptation


def adapt_event_value_in_stream_out(method):
  def adaptation(response_consumer, operation_context):
    def on_payload(payload):
      method.service(
          payload, response_consumer, _control.RpcContext(operation_context))
    return _control.UnaryConsumer(on_payload)
  return adaptation


def adapt_event_stream_in_value_out(method):
  def adaptation(response_consumer, operation_context):
    rpc_context = _control.RpcContext(operation_context)
    return method.service(response_consumer.consume_and_terminate, rpc_context)
  return adaptation


def adapt_event_stream_in_stream_out(method):
  def adaptation(response_consumer, operation_context):
    return method.service(
        response_consumer, _control.RpcContext(operation_context))
  return adaptation
