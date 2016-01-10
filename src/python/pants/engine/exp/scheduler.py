# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import collections
import inspect
import itertools
from abc import abstractmethod, abstractproperty
from collections import defaultdict, namedtuple

import six
from twitter.common.collections import OrderedSet

from pants.build_graph.address import Address
from pants.engine.exp.addressable import extract_config_selector
from pants.engine.exp.configuration import StructWithDeps
from pants.engine.exp.objects import Serializable
from pants.engine.exp.products import Products, lift_native_product
from pants.util.memo import memoized_property
from pants.util.meta import AbstractClass


class Select(namedtuple('Select', ['selector', 'input_product'])):

  class Subject(object):
    """Selects the Subject provided to the selector."""
    pass

  class Dependencies(namedtuple('Dependencies', ['deps_product'])):
    """Selects the dependencies of a product for the Subject."""
    pass

  class LiteralSubject(namedtuple('LiteralSubject', ['address'])):
    """Selects a literal Subject (other than the one applied to the selector)."""
    pass


class SelectionType(object):
  class Direct(SelectionType):
    """The result of the selection will be a single value."""
    pass

  class Dependencies(namedtuple('Dependencies', ['product']), SelectionType):
    """The result of the selection will be a list of values."""
    pass


class Subject(object):
  """The subject of a production plan."""

  @classmethod
  def as_subject(cls, item):
    """Return the given item as the primary of a subject if its not already a subject.

    :rtype: :class:`Subject`
    """
    return item if isinstance(item, Subject) else cls(primary=item)

  @classmethod
  def iter_configured_dependencies(cls, subject):
    """Return an iterator of the given subject's dependencies including any selected configurations.

    If no configuration is selected by a dependency (there is no `@[config-name]` specifier suffix),
    then `None` is returned for the paired configuration object; otherwise the `[config-name]` is
    looked for in the subject `configurations` list and returned if found or else an error is
    raised.

    :returns: An iterator over subjects dependencies as pairs of (dependency, configuration).
    :rtype: :class:`collections.Iterator` of (object, string)
    :raises: :class:`TaskPlanner.Error` if a dependency configuration was selected by subject but
             could not be found or was not unique.
    """
    for derivation in Subject.as_subject(subject).iter_derivations:
      if getattr(derivation, 'configurations', None):
        for config in derivation.configurations:
          if isinstance(config, StructWithDeps):
            for dep in config.dependencies:
              configuration = None
              if dep.address:
                config_specifier = extract_config_selector(dep.address)
                if config_specifier:
                  if not dep.configurations:
                    raise cls.Error('The dependency of {dependee} on {dependency} selects '
                                    'configuration {config} but {dependency} has no configurations.'
                                    .format(dependee=derivation,
                                            dependency=dep,
                                            config=config_specifier))
                  configuration = dep.select_configuration(config_specifier)
              yield dep, configuration

  @classmethod
  def native_products_for_subject(self, subject):
    """Return the products that are concretely present for the given subject."""
    if isinstance(subject, Target):
      # Config products.
      for configuration in subject.configurations:
        yield configuration
    else:
      # Any other type of subject is itself a product.
      yield subject

  def __init__(self, primary, alternate=None):
    """
    :param primary: The primary subject of a production plan.
    :param alternate: An alternate subject as suggested by some other plan.
    """
    self._primary = primary
    self._alternate = alternate

  @property
  def primary(self):
    """Return the primary subject."""
    return self._primary

  @property
  def iter_derivations(self):
    """Iterates over all subjects.

    The primary subject will always be returned as the 1st item from the iterator and if there is
    an alternate, it will be returned next.

    :rtype: :class:`collection.Iterator`
    """
    yield self._primary
    if self._alternate:
      yield self._alternate

  def __hash__(self):
    return hash(self._primary)

  def __eq__(self, other):
    return isinstance(other, Subject) and self._primary == other._primary

  def __ne__(self, other):
    return not (self == other)

  def __repr__(self):
    return 'Subject(primary={!r}, alternate={!r})'.format(self._primary, self._alternate)


class SchedulingError(Exception):
  """Indicates inability to make a scheduling promise."""


class NoProducersError(SchedulingError):
  """Indicates no planners were able to promise a product for a given subject."""

  def __init__(self, product_type, subject=None, configuration=None):
    msg = ('No plans to generate {!r}{} could be made.'
            .format(product_type.__name__,
                    ' for {!r}'.format(subject) if subject else '',
                    ' (with config {!r})' if configuration else ''))
    super(NoProducersError, self).__init__(msg)


class PartiallyConsumedInputsError(SchedulingError):
  """No planner was able to produce a plan that consumed the given input products."""

  @staticmethod
  def msg(output_product, subject, partially_consumed_products):
    yield 'While attempting to produce {} for {}, some products could not be consumed:'.format(
             output_product.__name__, subject)
    for input_product, planners in partially_consumed_products.items():
      yield '  To consume {}:'.format(input_product)
      for planner, additional_inputs in planners.items():
        inputs_str = ' OR '.join(str(i) for i in additional_inputs)
        yield '    {} needed ({})'.format(type(planner).__name__, inputs_str)

  def __init__(self, output_product, subject, partially_consumed_products):
    msg = '\n'.join(self.msg(output_product, subject, partially_consumed_products))
    super(PartiallyConsumedInputsError, self).__init__(msg)


class ConflictingProducersError(SchedulingError):
  """Indicates more than one planner was able to promise a product for a given subject.

  TODO: This will need to be legal in order to support multiple Planners producing a
  (mergeable) Classpath for one subject, for example.
  """

  def __init__(self, product_type, subject, plans):
    msg = ('Collected the following plans for generating {!r} from {!r}:\n\t{}'
            .format(product_type.__name__,
                    subject,
                    '\n\t'.join(str(p.func_or_task_type.value) for p in plans)))
    super(ConflictingProducersError, self).__init__(msg)


class ProductGraph(object):

  class State(Serializable):
    @classmethod
    def raise_unrecognized(cls, state):
      raise ValueError('Unrecognized Node State: {}'.format(state))

    class Return(namedtuple('Return', ['value']), State):
      pass

    class Throw(namedtuple('Throw', ['exception']), State):
      pass

    class Waiting(namedtuple('Waiting', ['dependencies']), State):
      """Indicates that a Node is waiting for some/all of the dependencies to become available.

      Some Nodes will return different dependency Nodes based on where they are in their lifecycle.
      """
      pass

  class Node(Serializable):
    @abstractproperty
    def subject(self):
      """The subject for this Node."""

    @abstractproperty
    def product(self):
      """The product for this Node."""

    @abstractmethod
    def step(self, dependency_states):
      """Given a dict of the dependency States for this Node, returns the current State of the Node.
      
      After this method returns a non-Waiting state, it will never be visited again for this Node.
      """

    class Select(namedtuple('Select', ['subject', 'product']), Node):
      """A Node that selects a product for a subject.
      
      A Select can be satisfied by multiple sources.
      """

      def _dependencies(self):
        """Returns a sequence of potential source Nodes for this Select."""
        # Look for native sources.
        yield ProductGraph.Node.Native(subject, product)
        # And for Tasks.
        for task, anded_clause in self._tasks[product_type]:
          yield ProductGraph.Node.Task(subject, product, task, anded_clause)

      def step(self, dependency_states):
        # If there are any Return Nodes, return the first.
        has_waiting_dep = False
        for dep in self._dependencies:
          dep_state = dependency_states.get(dep, None)
          if dep_state is None or type(dep_state) == State.Waiting:
            has_waiting_dep = True
          elif type(dep_state) == State.Return:
            return dep_state
        if has_waiting_dep:
          return State.Waiting(list(self._dependencies))
        else:
          return State.Throw(ValueError('No source of {}'.format(self.key)))

    class SelectDependencies(namedtuple('SelectDependencies', ['subject', 'product', 'dep_product']), Node):
      """A Node that selects products for the dependencies of a product.

      Begins by selecting the `dep_product` for the subject, and then selects a product for each
      of dep_products' dependencies.
      """
      def _dep_product_node(self):
        return Node.Select(subject, dep_product)

      def step(self, dependency_states):
        dep_product_state = dependency_states.get(self._dep_product_key(), None)
        if dep_product_state is None or type(dep_product_state) == State.Waiting:
          # Wait for the product which hosts the dependency list we need.
          return State.Waiting([self._dep_product_node()])
        elif type(dep_product_state) == State.Throw:
          msg = 'Could not compute {}, {} to determine dependencies.'.format(subject, dep_product)
          return State.Throw(ValueError(msg))
        elif type(dep_product_state) == State.Return:
          # The product and its dependency list are available.
          dependencies = [Node.Select(d, product) for d in dep_product_state.value.dependencies]
          for dependency in dependencies:
            dep_state = dependency_states.get(dependency, None)
            if dep_state is None or type(dep_state) == State.Waiting:
              # One of the dependencies is not yet available. Indicate that we are waiting for all
              # of them.
              return State.Waiting([self._dep_product_node()] + dependencies)
            elif type(dep_state) == State.Throw:
              msg = 'Failed to compute dependency of {}'.format(self._dep_product_key())
              return State.Throw(ValueError(msg))
            elif type(dep_state) != State.Return:
              raise State.raise_unrecognized(dep_state)
          # All dependencies are present! Set our value to a list of the resulting values.
          return State.Return([dependency_states[d].value for d in dependencies])
        else:
          State.raise_unrecognized(dep_state)

    class Task(namedtuple('Task', ['subject', 'product', 'func', 'clause']), Node):
      @property
      def _dependencies(self):
        for select in self.clause:
          if isinstance(select.selector, Select.Subject):
            yield Node.Select(subject, select.product)
          elif isinstance(select.selector, Select.Dependencies):
            yield Node.SelectDependencies(subject, select.product, select.selector.deps_product)
          elif isinstance(select.selector, Select.LiteralSubject):
            yield Node.Select(selector.address, product)
          else:
            raise ValueError('Unimplemented `Select` type: {}'.format(select))

      def step(self, dependency_states):
        # If all dependency Nodes are Return, execute the Node.
        dep_values = []
        for dep_key in self._dependencies:
          dep_state = dependency_states.get(dep_key, None)
          if dep_state is None:
            return State.Waiting(self._dependencies)
          elif type(dep_state) == Return:
            dep_values.append(dep_state.value)
          elif type(dep_state) == Failure:
            return State.Failure(ValueError('Dependency {} failed.'.format(dep_key)))
          else:
            State.raise_unrecognized(dep_state)
        try:
          return State.Return(func(*dep_values))
        except e:
          return State.Failure(e)

    class Native(namedtuple('Native', ['subject', 'product']), Node):
      def step(self, dependency_states):
        if type(subject) == product:
          return State.Return(subject)
        elif isinstance(subject, Target):
          for configuration in subject.configurations:
            # TODO: returning only the first configuration of a given type. Need to define mergeability
            # for products.
            if type(configuration) == product:
              return State.Return(configuration)
        return State.Throw(ValueError('No native source of {} for {}'.format(self.product, self.subject)))

  def __init__(self, roots):
    self._roots = tuple(roots)
    # A dict from Node to its computed value: if a Node hasn't been computed yet, it will not
    # be present here.
    self._node_results = dict()
    # A dict from Node to list of dependency Nodes.
    self._nodes = set(roots)

  @property
  def roots(self):
    return self._roots

  def sources_for(self, subject, product, consumed_product=None):
    """Yields the set of Sources for the given subject and product (which consume the given config).

    :param subject: The subject that the product will be produced for.
    :param type product: The product type the returned planners are capable of producing.
    :param consumed_product: An optional configuration to require that a planner consumes, or None.
    :rtype: sequences of ProductGraph.Node. instances.
    """

    def consumes_product(node):
      """Returns True if the given Node recursively consumes the given product.

      TODO: This is matching on type only, while selectors are usually implemented
      as by-name. Convert config selectors to configuration mergers.
      """
      if not consumed_product:
        return True
      for dep_node in self._adjacencies[node]:
        if dep_node.product == type(consumed_product):
          return True
        elif consumes_product(dep_node):
          return True
      return False

    key = (subject, product)
    # TODO: order N: index by subject
    for node in self._nodes:
      # Yield Sources that were recursively able to consume the configuration.
      if isinstance(node.source, ProductGraph.Node.OR):
        continue
      if node.key == key and self._is_satisfiable(node) and consumes_product(node):
        yield node.source

  def products_for(self, subject):
    """Returns a set of products that are possible to produce for the given subject."""
    products = set()
    # TODO: order N: index by subject
    for node in self._nodes:
      if node.subject == subject and self._is_satisfiable(node):
        products.add(node.product)
    return products


class Planners(object):
  """A registry of task planners indexed by both product type and goal name.

  Holds a set of input product requirements for each output product, which can be used
  to validate the graph.
  """

  def __init__(self, products_by_goal, tasks):
    self._products_by_goal = products_by_goal
    self._tasks = defaultdict(set)

    # Index tasks by their output type.
    for output_type, input_type_requirements, task in tasks:
      self._tasks[output_type].add((task, tuple(input_type_requirements)))

  def products_for_goal(self, goal_name):
    """Return the set of products required for the given goal.

    :param string goal_name:
    :rtype: set of product types
    """
    return self._products_by_goal[goal_name]

  def product_graph(self, build_graph, root_subjects, root_products):
    """Bootstraps a product graph for the given root subjects and products."""
    roots = [Node.Select(s, p) for s in root_subjects for p in root_products]
    return ProductGraph(roots)


class BuildRequest(object):
  """Describes the user-requested build."""

  def __init__(self, goals, addressable_roots):
    """
    :param goals: The list of goal names supplied on the command line.
    :type goals: list of string
    :param addressable_roots: The list of addresses supplied on the command line.
    :type addressable_roots: list of :class:`pants.build_graph.address.Address`
    """
    self._goals = goals
    self._addressable_roots = addressable_roots

  @property
  def goals(self):
    """Return the list of goal names supplied on the command line.

    :rtype: list of string
    """
    return self._goals

  @property
  def addressable_roots(self):
    """Return the list of addresses supplied on the command line.

    :rtype: list of :class:`pants.build_graph.address.Address`
    """
    return self._addressable_roots

  def __repr__(self):
    return ('BuildRequest(goals={!r}, addressable_roots={!r})'
            .format(self._goals, self._addressable_roots))


# A synthetic planner that lifts products defined directly on targets into the product
# namespace.
class NoPlanner(TaskPlanner):
  @classmethod
  def finalize_plans(cls, plans):
    return plans


class ProductMapper(object):
  """Stores the mapping from promises to the plans whose execution will satisfy them."""

  class InvalidRegistrationError(Exception):
    """Indicates registration of a plan that does not cover the expected subject."""

  def __init__(self, graph, product_graph):
    self._graph = graph
    self._product_graph = product_graph
    self._promises = {}
    self._plans_by_product_type_by_planner = defaultdict(lambda: defaultdict(OrderedSet))

  def _register_promises(self, product_type, plan, primary_subject=None, configuration=None):
    """Registers the promises the given plan will satisfy when executed.

    :param type product_type: The product type the plan will produce when executed.
    :param plan: The plan to register promises for.
    :type plan: :class:`Plan`
    :param primary_subject: An optional primary subject.  If supplied, the registered promise for
                            this subject will be returned.
    :param object configuration: An optional promised configuration.
    :returns: The promise for the primary subject of one was supplied.
    :rtype: :class:`Promise`
    :raises: :class:`ProductMapper.InvalidRegistrationError` if a primary subject was supplied but
             not a member of the given plan's subjects.
    """
    # Index by all subjects.  This allows dependencies on products from "chunking" tasks, even
    # products from tasks that act globally in the extreme.
    primary_promise = None
    for subject in plan.subjects:
      promise = Promise(product_type, subject, configuration=configuration)
      if primary_subject == subject.primary:
        primary_promise = promise
      self._promises[promise] = plan

    if primary_subject and not primary_promise:
      raise self.InvalidRegistrationError('The subject {} is not part of the final plan!: {}'
                                          .format(primary_subject, plan))
    return primary_promise

  def promised(self, promise):
    """Return the plan that was promised.

    :param promise: The promise to lookup a registered plan for.
    :type promise: :class:`Promise`
    :returns: The plan registered for the given promise; or `None`.
    :rtype: :class:`Plan`
    """
    return self._promises.get(promise)

  def promise(self, subject, product_type, configuration=None):
    """Return a promise for a product of the given `product_type` for the given `subject`.

    The subject can either be a :class:`pants.engine.exp.objects.Serializable` object or else an
    :class:`Address`, in which case the promise subject is the addressable, serializable object it
    points to.

    If a configuration is supplied, the promise is for the requested product in that configuration.

    If no production plans can be made a :class:`SchedulingError` is raised.

    :param object subject: The subject that the product type should be created for.
    :param type product_type: The type of product to promise production of for the given subject.
    :param object configuration: An optional requested configuration for the product.
    :returns: A promise to make the given product type available for subject at task execution time.
    :rtype: :class:`Promise`
    :raises: :class:`SchedulingError` if no production plans could be made.
    """
    if isinstance(subject, Address):
      subject = self._graph.resolve(subject)

    promise = Promise(product_type, subject, configuration=configuration)
    plan = self.promised(promise)
    if plan is not None:
      return promise

    plans = []
    # For all sources of the product, request it.
    for source in self._product_graph.sources_for(subject,
                                                  product_type,
                                                  consumed_product=configuration):
      if isinstance(source, ProductGraph.Node.Native):
        plans.append((NoPlanner(), Plan(func_or_task_type=lift_native_product,
                                        subjects=(subject,),
                                        subject=subject,
                                        product_type=product_type)))
      elif isinstance(source, ProductGraph.Node.Task):
        plan = source.planner.plan(self, product_type, subject, configuration=configuration)
        # TODO: remove None check... there should no longer be any planners failing.
        if plan:
          plans.append((source.planner, plan))
      else:
        raise ValueError('Unsupported source for ({}, {}): {}'.format(
          subject, product_type, source))

    # TODO: It should be legal to have multiple plans, and they should be merged.
    if len(plans) > 1:
      raise ConflictingProducersError(product_type, subject, [plan for _, plan in plans])
    elif not plans:
      raise NoProducersError(product_type, subject, configuration)

    planner, plan = plans[0]
    try:
      primary_promise = self._register_promises(product_type, plan,
                                                primary_subject=subject,
                                                configuration=configuration)
      self._plans_by_product_type_by_planner[planner][product_type].add(plan)
      return primary_promise
    except ProductMapper.InvalidRegistrationError:
      raise SchedulingError('The plan produced for {subject!r} by {planner!r} does not cover '
                            '{subject!r}:\n\t{plan!r}'.format(subject=subject,
                                                              planner=type(planner).__name__,
                                                              plan=plan))


class LocalScheduler(object):
  """A scheduler that formulates an execution graph locally."""

  # TODO(John Sirois): Allow for subject-less (target-less) goals.  Examples are clean-all,
  # ng-killall, and buildgen.go.
  #
  # 1. If not subjects check for a special Planner subtype with a special subject-less
  #    promise method.
  # 2. Use a sentinel NO_SUBJECT, planners that care test for this, other planners that
  #    looks for Target or Jar or ... will naturally just skip it and no-op.
  #
  # Option 1 allows for failing the build if no such subtypes are amongst the goals;
  # ie: `./pants compile` would fail since there are no inputs and all compile registered
  # planners require subjects (don't implement the subtype).
  # Seems promising - but what about mixed goals and no subjects?
  #
  # What about if subjects but the planner doesn't care about them?  Is using the IvyGlobal
  # trick good enough here?  That pattern with fake Plans to aggregate could be packaged in
  # a TaskPlanner baseclass.

  def __init__(self, graph, planners, build_request):
    """
    :param graph: The BUILD graph build requests will execute against.
    :type graph: :class:`pants.engine.exp.graph.Graph`
    :param planners: All the task planners known to the system.
    :type planners: :class:`Planners`
    """
    self._graph = graph
    self._planners = planners
    self._build_request = build_request


    # Determine the root products and subjects based on the request and specified goals.
    root_subjects = [self._graph.resolve(a) for a in build_request.addressable_roots]
    root_products = OrderedSet()
    for goal in build_request.goals:
      root_products.update(self._planners.products_for_goal(goal))

    # Compute a ProductGraph that determines which products are possible to produce for
    # these subjects.
    product_graph = self._planners.product_graph(self._graph, root_subjects, root_products)
    #print('>>>\n{}'.format('\n'.join(product_graph.edge_strings())))
    product_mapper = ProductMapper(self._graph, product_graph)

    # Track the root promises for relevant products for each subject.
    root_promises = []
    for subject in root_subjects:
      relevant_products = root_products & set(product_graph.products_for(subject))
      for product_type in relevant_products:
        root_promises.append(product_mapper.promise(subject, product_type))
    self._root_promises = tuple(root_promises)

  def schedule(self):
    """Determines which Promises are ready to execute.
    
    Each Promise is returned with an execution Plan containing the dependencies.
    """


    # Give aggregating planners a chance to aggregate plans.
    product_mapper.aggregate_plans()

    return ExecutionGraph(root_promises, product_mapper)

  def root_promises(self):
    return self._root_promises
