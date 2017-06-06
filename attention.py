import theano
from theano import tensor

from blocks.bricks import (Brick, Initializable, Sequence,
                           Feedforward, Linear, Tanh)
from blocks.bricks.base import lazy, application
from blocks.bricks.parallel import Parallel, Distribute
from blocks.bricks.recurrent import recurrent, BaseRecurrent
from blocks.utils import dict_union, dict_subset, pack
from blocks.bricks.attention import (
    GenericSequenceAttention, AbstractAttentionRecurrent)
from match_functions import SumMatchFunction


class SequenceContentAttention(GenericSequenceAttention, Initializable):
    """Attention mechanism that looks for relevant content in a sequence.

    This is the attention mechanism used in [BCB]_. The idea in a nutshell:

    1. The states and the sequence are transformed independently,

    2. The transformed states are summed with every transformed sequence
       element to obtain *match vectors*,

    3. A match vector is transformed into a single number interpreted as
       *energy*,

    4. Energies are normalized in softmax-like fashion. The resulting
       summing to one weights are called *attention weights*,

    5. Weighted average of the sequence elements with attention weights
       is computed.

    In terms of the :class:`AbstractAttention` documentation, the sequence
    is the attended. The weighted averages from 5 and the attention
    weights from 4 form the set of glimpses produced by this attention
    mechanism.

    Parameters
    ----------
    state_names : list of str
        The names of the network states.
    attended_dim : int
        The dimension of the sequence elements.
    match_dim : int
        The dimension of the match vector.
    state_transformer : :class:`.Brick`
        A prototype for state transformations. If ``None``,
        a linear transformation is used.
    attended_transformer : :class:`.Feedforward`
        The transformation to be applied to the sequence. If ``None`` an
        affine transformation is used.
    energy_computer : :class:`.Feedforward`
        Computes energy from the match vector. If ``None``, an affine
        transformations preceeded by :math:`tanh` is used.

    Notes
    -----
    See :class:`.Initializable` for initialization parameters.

    .. [BCB] Dzmitry Bahdanau, Kyunghyun Cho and Yoshua Bengio. Neural
       Machine Translation by Jointly Learning to Align and Translate.

    """
    @lazy(allocation=['match_dim'])
    def __init__(self, match_dim,
                 use_local_attention=False, window_size=10, sigma=None,
                 state_transformer=None, local_state_transformer=None,
                 local_predictor=None, attended_transformer=None,
                 energy_computer=None, **kwargs):
        super(SequenceContentAttention, self).__init__(**kwargs)
        if not state_transformer:
            state_transformer = Linear(use_bias=False, name="state_trans")
        if not local_state_transformer:
            local_state_transformer = Linear(use_bias=False,
                                             name="local_state_trans")
        if not local_predictor:
            local_predictor = Linear(use_bias=False, name="local_pred")
        if sigma is None:
            sigma = window_size * 1.0 / 2
        self.use_local_attention = use_local_attention
        self.sigma = sigma * sigma
        self.match_dim = match_dim
        self.state_name = self.state_names[0]

        self.state_transformer = state_transformer
        self.local_state_transformer = local_state_transformer
        self.local_predictor = local_predictor

        if not attended_transformer:
            attended_transformer = Linear(name="preprocess")
        if not energy_computer:
            energy_computer = SumMatchFunction(name="energy_comp")
        self.attended_transformer = attended_transformer
        self.energy_computer = energy_computer

        self.children = [self.state_transformer, self.local_state_transformer,
                         self.local_predictor, self.attended_transformer,
                         energy_computer]

    def _push_allocation_config(self):
        self.state_dim = self.state_dims[0]

        self.state_transformer.input_dim = self.state_dim
        self.state_transformer.output_dim = self.match_dim

        self.local_state_transformer.input_dim = self.state_dim
        self.local_state_transformer.output_dim = self.match_dim

        self.local_predictor.input_dim = self.state_dim
        self.local_predictor.output_dim = 1

        self.attended_transformer.input_dim = self.attended_dim
        self.attended_transformer.output_dim = self.match_dim

        self.energy_computer.input_dim = self.match_dim
        self.energy_computer.output_dim = 1

    @application
    def compute_energies(self, attended, preprocessed_attended, states):
        if not preprocessed_attended:
            preprocessed_attended = self.preprocess(attended)
        _states = states[self.state_name]
        transformed_states = self.state_transformer.apply(_states)
        # Broadcasting of transformed states should be done automatically
        # match_vectors = sum(transformed_states.values(),
        #                     preprocessed_attended)
        # energies = self.energy_computer.apply(match_vectors).reshape(
        #     match_vectors.shape[:-1], ndim=match_vectors.ndim - 1)
        energies = self.energy_computer.apply(transformed_states,
                                              preprocessed_attended)
        return energies

    @application
    def get_local_predition(self, states, attended, attended_mask):
        _states = states[self.state_name]
        # local_states: [batch, features]
        local_states = self.local_state_transformer.apply(_states)
        # local_prediction is reshaped to [batch]
        local_prediction = self.local_predictor.apply(
            tensor.tanh(local_states)).reshape(
                local_states.shape[:-1], ndim=local_states.ndim - 1)
        local_prediction = tensor.nnet.sigmoid(local_prediction)
        # attended_mask is [time, batch]
        _attended_mask = tensor.sum(attended_mask, axis=0)
        return _attended_mask * local_prediction

    @application
    def adjust_weights(self, attended_mask, weights, local_prediction):
        # weights: [time, batch]
        # local_prediction: [batch]
        # locations: [time, batch]
        locations = tensor.arange(
            attended_mask.shape[0]).repeat(
                attended_mask.shape[1]).reshape(
                    attended_mask.shape).astype(
                        theano.config.floatX)
        # diff: [time, batch]
        diff = locations - local_prediction
        # gauss: [time, batch]
        gauss = tensor.pow(diff, 2) / (2 * self.sigma)
        gauss = tensor.exp(-gauss)
        weights = weights * gauss
        return weights

    @application(outputs=['weighted_averages', 'weights'])
    def take_glimpses(self, attended, preprocessed_attended=None,
                      attended_mask=None, **states):
        r"""Compute attention weights and produce glimpses.

        Parameters
        ----------
        attended : :class:`~tensor.TensorVariable`
            The sequence, time is the 1-st dimension.
        preprocessed_attended : :class:`~tensor.TensorVariable`
            The preprocessed sequence. If ``None``, is computed by calling
            :meth:`preprocess`.
        attended_mask : :class:`~tensor.TensorVariable`
            A 0/1 mask specifying available data. 0 means that the
            corresponding sequence element is fake.
        \*\*states
            The states of the network.

        Returns
        -------
        weighted_averages : :class:`~theano.Variable`
            Linear combinations of sequence elements with the attention
            weights.
        weights : :class:`~theano.Variable`
            The attention weights. The first dimension is batch, the second
            is time.

        """
        energies = self.compute_energies(
            attended, preprocessed_attended, states)
        # weights has dimensions: [time (src), batch]
        weights = self.compute_weights(energies, attended_mask)
        if self.use_local_attention:
            # local_pred should have dimension: [batch],
            # the predicted position for each batch
            local_pred = self.get_local_predition(
                states, attended, attended_mask)
            weights = self.adjust_weights(attended_mask, weights, local_pred)
        weighted_averages = self.compute_weighted_averages(weights, attended)
        return weighted_averages, weights.T

    @take_glimpses.property('inputs')
    def take_glimpses_inputs(self):
        return (['attended', 'preprocessed_attended', 'attended_mask'] +
                self.state_names)

    @application(outputs=['weighted_averages', 'weights'])
    def initial_glimpses(self, batch_size, attended):
        return [tensor.zeros((batch_size, self.attended_dim)),
                tensor.zeros((batch_size, attended.shape[0]))]

    @application(inputs=['attended'], outputs=['preprocessed_attended'])
    def preprocess(self, attended):
        """Preprocess the sequence for computing attention weights.

        Parameters
        ----------
        attended : :class:`~tensor.TensorVariable`
            The attended sequence, time is the 1-st dimension.

        """
        return self.attended_transformer.apply(attended)

    def get_dim(self, name):
        if name in ['weighted_averages','topical_weighted_averages','topical_weights']:
            return self.attended_dim
        if name in ['weights']:
            return 0
        return super(SequenceContentAttention, self).get_dim(name)


class AttentionRecurrent(AbstractAttentionRecurrent, Initializable):
    """Combines an attention mechanism and a recurrent transition.

    This brick equips a recurrent transition with an attention mechanism.
    In order to do this two more contexts are added: one to be attended and
    a mask for it. It is also possible to use the contexts of the given
    recurrent transition for these purposes and not add any new ones,
    see `add_context` parameter.

    At the beginning of each step attention mechanism produces glimpses;
    these glimpses together with the current states are used to compute the
    next state and finish the transition. In some cases glimpses from the
    previous steps are also necessary for the attention mechanism, e.g.
    in order to focus on an area close to the one from the previous step.
    This is also supported: such glimpses become states of the new
    transition.

    To let the user control the way glimpses are used, this brick also
    takes a "distribute" brick as parameter that distributes the
    information from glimpses across the sequential inputs of the wrapped
    recurrent transition.

    Parameters
    ----------
    transition : :class:`.BaseRecurrent`
        The recurrent transition.
    attention : :class:`.Brick`
        The attention mechanism.
    distribute : :class:`.Brick`, optional
        Distributes the information from glimpses across the input
        sequences of the transition. By default a :class:`.Distribute` is
        used, and those inputs containing the "mask" substring in their
        name are not affected.
    add_contexts : bool, optional
        If ``True``, new contexts for the attended and the attended mask
        are added to this transition, otherwise existing contexts of the
        wrapped transition are used. ``True`` by default.
    attended_name : str
        The name of the attended context. If ``None``, "attended"
        or the first context of the recurrent transition is used
        depending on the value of `add_contents` flag.
    attended_mask_name : str
        The name of the mask for the attended context. If ``None``,
        "attended_mask" or the second context of the recurrent transition
        is used depending on the value of `add_contents` flag.

    Notes
    -----
    See :class:`.Initializable` for initialization parameters.

    Wrapping your recurrent brick with this class makes all the
    states mandatory. If you feel this is a limitation for you, try
    to make it better! This restriction does not apply to sequences
    and contexts: those keep being as optional as they were for
    your brick.

    Those coming to Blocks from Groundhog might recognize that this is
    a `RecurrentLayerWithSearch`, but on steroids :)

    """
    def __init__(self, transition, attention, distribute=None,
                 add_contexts=True,
                 attended_name=None, attended_mask_name=None,
                 **kwargs):
        super(AttentionRecurrent, self).__init__(**kwargs)
        self._sequence_names = list(transition.apply.sequences)
        self._state_names = list(transition.apply.states)
        self._context_names = list(transition.apply.contexts)
        if add_contexts:
            if not attended_name:
                attended_name = 'attended'
            if not attended_mask_name:
                attended_mask_name = 'attended_mask'
            self._context_names += [attended_name, attended_mask_name]
        else:
            attended_name = self._context_names[0]
            attended_mask_name = self._context_names[1]
        if not distribute:
            normal_inputs = [name for name in self._sequence_names
                             if 'mask' not in name]
            distribute = Distribute(normal_inputs,
                                    attention.take_glimpses.outputs[0])

        self.transition = transition
        self.attention = attention
        self.distribute = distribute
        self.add_contexts = add_contexts
        self.attended_name = attended_name
        self.attended_mask_name = attended_mask_name

        self.preprocessed_attended_name = "preprocessed_" + self.attended_name

        self._glimpse_names = self.attention.take_glimpses.outputs
        # We need to determine which glimpses are fed back.
        # Currently we extract it from `take_glimpses` signature.
        self.previous_glimpses_needed = [
            name for name in self._glimpse_names
            if name in self.attention.take_glimpses.inputs]

        self.children = [self.transition, self.attention, self.distribute]

    def _push_allocation_config(self):
        self.attention.state_dims = self.transition.get_dims(
            self.attention.state_names)
        self.attention.attended_dim = self.get_dim(self.attended_name)
        self.distribute.source_dim = self.attention.get_dim(
            self.distribute.source_name)
        self.distribute.target_dims = self.transition.get_dims(
            self.distribute.target_names)

    @application
    def take_glimpses(self, **kwargs):
        r"""Compute glimpses with the attention mechanism.

        A thin wrapper over `self.attention.take_glimpses`: takes care
        of choosing and renaming the necessary arguments.

        Parameters
        ----------
        \*\*kwargs
            Must contain the attended, previous step states and glimpses.
            Can optionaly contain the attended mask and the preprocessed
            attended.

        Returns
        -------
        glimpses : list of :class:`~tensor.TensorVariable`
            Current step glimpses.

        """
        states = dict_subset(kwargs, self._state_names, pop=True)
        glimpses = dict_subset(kwargs, self._glimpse_names, pop=True)
        glimpses_needed = dict_subset(glimpses, self.previous_glimpses_needed)
        result = self.attention.take_glimpses(
            kwargs.pop(self.attended_name),
            kwargs.pop(self.preprocessed_attended_name, None),
            kwargs.pop(self.attended_mask_name, None),
            **dict_union(states, glimpses_needed))
        # At this point kwargs may contain additional items.
        # e.g. AttentionRecurrent.transition.apply.contexts
        return result

    @take_glimpses.property('outputs')
    def take_glimpses_outputs(self):
        return self._glimpse_names

    @application
    def compute_states(self, **kwargs):
        r"""Compute current states when glimpses have already been computed.

        Combines an application of the `distribute` that alter the
        sequential inputs of the wrapped transition and an application of
        the wrapped transition. All unknown keyword arguments go to
        the wrapped transition.

        Parameters
        ----------
        \*\*kwargs
            Should contain everything what `self.transition` needs
            and in addition the current glimpses.

        Returns
        -------
        current_states : list of :class:`~tensor.TensorVariable`
            Current states computed by `self.transition`.

        """
        # make sure we are not popping the mask
        normal_inputs = [name for name in self._sequence_names
                         if 'mask' not in name]
        sequences = dict_subset(kwargs, normal_inputs, pop=True)
        glimpses = dict_subset(kwargs, self._glimpse_names, pop=True)
        if self.add_contexts:
            kwargs.pop(self.attended_name)
            # attended_mask_name can be optional
            kwargs.pop(self.attended_mask_name, None)

        sequences.update(self.distribute.apply(
            as_dict=True, **dict_subset(dict_union(sequences, glimpses),
                                        self.distribute.apply.inputs)))
        current_states = self.transition.apply(
            iterate=False, as_list=True,
            **dict_union(sequences, kwargs))
        return current_states

    @compute_states.property('outputs')
    def compute_states_outputs(self):
        return self._state_names

    @recurrent
    def do_apply(self, **kwargs):
        r"""Process a sequence attending the attended context every step.

        In addition to the original sequence this method also requires
        its preprocessed version, the one computed by the `preprocess`
        method of the attention mechanism. Unknown keyword arguments
        are passed to the wrapped transition.

        Parameters
        ----------
        \*\*kwargs
            Should contain current inputs, previous step states, contexts,
            the preprocessed attended context, previous step glimpses.

        Returns
        -------
        outputs : list of :class:`~tensor.TensorVariable`
            The current step states and glimpses.

        """
        attended = kwargs[self.attended_name]
        preprocessed_attended = kwargs.pop(self.preprocessed_attended_name)
        attended_mask = kwargs.get(self.attended_mask_name)
        sequences = dict_subset(kwargs, self._sequence_names, pop=True,
                                must_have=False)
        states = dict_subset(kwargs, self._state_names, pop=True)
        glimpses = dict_subset(kwargs, self._glimpse_names, pop=True)

        current_glimpses = self.take_glimpses(
            as_dict=True,
            **dict_union(
                states, glimpses,
                {self.attended_name: attended,
                 self.attended_mask_name: attended_mask,
                 self.preprocessed_attended_name: preprocessed_attended}))
        current_states = self.compute_states(
            as_list=True,
            **dict_union(sequences, states, current_glimpses, kwargs))
        return current_states + list(current_glimpses.values())

    @do_apply.property('sequences')
    def do_apply_sequences(self):
        return self._sequence_names

    @do_apply.property('contexts')
    def do_apply_contexts(self):
        return self._context_names + [self.preprocessed_attended_name]

    @do_apply.property('states')
    def do_apply_states(self):
        return self._state_names + self._glimpse_names

    @do_apply.property('outputs')
    def do_apply_outputs(self):
        return self._state_names + self._glimpse_names

    @application
    def apply(self, **kwargs):
        """Preprocess a sequence attending the attended context at every step.

        Preprocesses the attended context and runs :meth:`do_apply`. See
        :meth:`do_apply` documentation for further information.

        """
        preprocessed_attended = self.attention.preprocess(
            kwargs[self.attended_name])
        return self.do_apply(
            **dict_union(kwargs,
                         {self.preprocessed_attended_name:
                          preprocessed_attended}))

    @apply.delegate
    def apply_delegate(self):
        # TODO: Nice interface for this trick?
        return self.do_apply.__get__(self, None)

    @apply.property('contexts')
    def apply_contexts(self):
        return self._context_names

    @application
    def initial_states(self, batch_size, **kwargs):
        return (pack(self.transition.initial_states(
                     batch_size, **kwargs)) +
                pack(self.attention.initial_glimpses(
                     batch_size, kwargs[self.attended_name])))

    @initial_states.property('outputs')
    def initial_states_outputs(self):
        return self.do_apply.states

    def get_dim(self, name):
        if name in self._glimpse_names:
            return self.attention.get_dim(name)
        if name == self.preprocessed_attended_name:
            (original_name,) = self.attention.preprocess.outputs
            return self.attention.get_dim(original_name)
        if self.add_contexts:
            if name == self.attended_name:
                return self.attention.get_dim(
                    self.attention.take_glimpses.inputs[0])
            if name == self.attended_mask_name:
                return 0
        return self.transition.get_dim(name)
