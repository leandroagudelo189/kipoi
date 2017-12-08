"""Defines the base classes
"""
import related
import numpy as np
import enum
import collections
from collections import OrderedDict
import kipoi.conda as kconda
from kipoi.utils import unique_list, yaml_ordered_dump
from kipoi.metadata import GenomicRanges
from kipoi.external.related.fields import StrSequenceField, NestedMappingField, TupleIntField, AnyField, UNSPECIFIED
import six
import logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
# --------------------------------------------
# TODO additionally validate the special type properties - ???

# Abstract classes & Mixins for models defined using related


class RelatedConfigMixin(object):
    """Provides from_config and get_config to @related.immutable decorated classes
    """
    @classmethod
    def from_config(cls, cfg):
        # TODO - create a nicer error message - see above
                # Verbose unrecognized field
        # for k in kwargs.keys():
        #     if k not in cls.REQ_FIELDS + cls.OPT_FIELDS:
        #         raise ValueError("Unrecognized field in info: '{f}'. Avaiable fields are: {l}".
        #                          format(f=k, l=cls.REQ_FIELDS))

        # # Verbose undefined field
        # undefined_set = set(cls.REQ_FIELDS) - kwargs.keys()
        # if undefined_set:
        #     raise ValueError("The following arguments were not specified: {0}. Please specify them.".
        #                      format(undefined_set))
        return related.to_model(cls, cfg)

    def get_config(self):
        return related.to_dict(self)


class RelatedLoadSaveMixin(RelatedConfigMixin):
    """Adds load and dump on top of RelatedConfigMixin for reading and writing from a yaml file
    """

    @classmethod
    def load(cls, path):
        """Loads model from a yaml file
        """
        original_yaml = open(path).read().strip()
        parsed_dict = related.from_yaml(original_yaml)
        if "path" not in parsed_dict:
            parsed_dict["path"] = path
        return cls.from_config(parsed_dict)

    def dump(self, path):
        """Dump the object to a yaml file
        """
        generated_yaml = related.to_yaml(self,
                                         suppress_empty_values=True,
                                         suppress_map_key_values=True)  # .strip()
        with open(path, "w") as f:
            f.write(generated_yaml)


# --------------------------------------------
# Common components (model and dataloader)

@related.immutable
class Author(RelatedConfigMixin):
    name = related.StringField()
    github = related.StringField(required=False)
    email = related.StringField(required=False)


@related.immutable
class Info(RelatedConfigMixin):
    """Class holding information about the component.
    Parses the info section in component.yaml:

    info:
      authors:
        - name: Ziga Avsec
      doc: RBP binding prediction
      name: rbp_eclip
      version: 0.1
    """
    authors = related.SequenceField(Author, repr=True)
    doc = related.StringField()
    name = related.StringField(required=False)  # TODO - deprecate
    version = related.StringField(default="0.1", required=False)
    tags = StrSequenceField(str, default=[], required=False)


@enum.unique
class ArraySpecialType(enum.Enum):
    DNASeq = "DNASeq"
    BIGWIG = "bigwig"
    VPLOT = "v-plot"
    Array = "Array"


@related.immutable
class ArraySchema(RelatedConfigMixin):
    """

    Args:
      shape: Tuple of shape (same as in Keras for the input)
      doc: Description of the array
      special_type: str, special type name. Could also be an array of special entries?
      metadata_entries: str or list of metadata
    """
    verbose = True
    shape = TupleIntField()
    doc = related.StringField()
    # MAYBE - allow a list of strings?
    #         - could be useful when a single array can have multiple 'attributes'
    name = related.StringField(required=False)
    special_type = related.ChildField(ArraySpecialType, required=False)
    associated_metadata = StrSequenceField(str, default=[], required=False)
    column_labels = StrSequenceField(str, default=[], required=False)  # either a list or a path to a file --> need to check whether it's a list
    # TODO shall we have
    # - associated_metadata in ArraySchema
    # OR
    # - associated_array in MetadataField?

    # assert that there are no Nones in the shape, assume that channels is the only 4 or it is the last
    # update the model schema shape on calling batch_iter method
    # overwrite the batch_iter method of the returned dataloader --> decorator needed

    def print_msg(self, msg):
        if self.verbose:
            print("ArraySchema mismatch")
            print(msg)

    def _validate_list_column_labels(self):
        dim_ok = len(self.shape) >= 1
        if dim_ok and (self.shape[0] is not None):
            dim_ok &= len(self.column_labels) == self.shape[0]
        if not dim_ok:
            self.print_msg("Column annotation does not match array dimension with shape %s and %d labels (%s ...)"
                           % (str(self.shape), len(self.column_labels), str(self.column_labels)[:30]))

    def __attrs_post_init__(self):
        if len(self.column_labels) > 1:
            # check that length is ok with columns
            self._validate_list_column_labels()
        elif len(self.column_labels) == 1:
            label = self.column_labels.list[0]
            import os
            # check if path exists raise exception only test time, but only a warning in prediction time
            if os.path.exists(label):
                with open(label, "r") as ifh:
                    object.__setattr__(self, "column_labels", [l.rstrip() for l in ifh])
            self._validate_list_column_labels()
        else:
            object.__setattr__(self, "column_labels", None)

    def compatible_with_batch(self, batch, verbose=True):
        """Checks compatibility with a particular batch of data

        Args:
          batch: numpy array
          ignore_batch_axis: if True, the batch axis is not considered
          verbose: print the fail reason
        """
        def print_msg(msg):
            if verbose:
                print("ArraySchema missmatch")
                print(msg)

        # type = np.ndarray
        if not isinstance(batch, np.ndarray):
            print_msg("Expecting a np.ndarray. Got type(batch) = {0}".format(type(batch)))
            return False

        if not batch.ndim >= 1:
            print_msg("The array is a scalar (expecting at least the batch dimension)")
            return False

        return self.compatible_with_schema(ArraySchema(shape=batch.shape[1:],
                                                       doc=""))

    def compatible_with_schema(self, schema, verbose=True):
        """Checks the compatibility with another schema
        """
        def print_msg(msg):
            if verbose:
                print("ArraySchema missmatch")
                print(msg)

        if not isinstance(schema, ArraySchema):
            print_msg("Expecting ArraySchema. Got type(schema) = {0}".format(type(schema)))
            return False

        def print_msg_template():
            print_msg("Array shapes don't match for : {0}".format(self))
            print_msg("Provided shape (without the batch axis): {0}, expected shape: {1} ".format(bshape, self.shape))

        bshape = schema.shape
        if not len(bshape) == len(self.shape):
            print_msg_template()
            return False
        for i in range(len(bshape)):
            if bshape[i] is not None and self.shape[i] is not None:
                # shapes don't match
                if not bshape[i] == self.shape[i]:
                    print_msg_template()
                    return False
        return True


# --------------------------------------------
# Model specific components

@related.immutable
class ModelSchema(RelatedConfigMixin):
    """Describes the model schema
    """
    # can be a dictionary, list or a single array
    inputs = NestedMappingField(ArraySchema, keyword="shape", key="name")
    targets = NestedMappingField(ArraySchema, keyword="shape", key="name")

    def compatible_with_schema(self, dataloader_schema, verbose=True):
        """Check the compatibility: model.schema <-> dataloader.output_schema

        Checks preformed:
        - nested structure is the same (i.e. dictionary names, list length etc)
        - array shapes are compatible
        - returned obj classess are compatible

        # Arguments
            dataloader_schema: a dataloader_schema of data returned by one iteraton of dataloader's dataloader_schema_iter
                nested dictionary
            verbose: verbose error logging if things don't match

        # Returns
           bool: True only if everyhing is ok
        """
        def print_msg(msg):
            if verbose:
                print(msg)

        # Inputs check
        def compatible_nestedmapping(dschema, descr, cls, verbose=True):
            """Recursive function of checks

            shapes match, dschema-dim matches
            """
            if isinstance(descr, cls):
                return descr.compatible_with_schema(dschema, verbose=verbose)
            elif isinstance(dschema, collections.Mapping) and isinstance(descr, collections.Mapping):
                if not set(descr.keys()).issubset(set(dschema.keys())):
                    print_msg("Dataloader doesn't provide all the fields required by the model:")
                    print_msg("dataloader fields: {0}".format(dschema.keys()))
                    print_msg("model fields: {0}".format(descr.keys()))
                    return False
                return all([compatible_nestedmapping(dschema[key], descr[key], cls, verbose) for key in descr])
            elif isinstance(dschema, collections.Sequence) and isinstance(descr, collections.Sequence):
                if not len(descr) <= len(dschema):
                    print_msg("Dataloader doesn't provide all the fields required by the model:")
                    print_msg("len(dataloader): {0}".format(len(dschema)))
                    print_msg("len(model): {0}".format(len(descr)))
                    return False
                return all([compatible_nestedmapping(dschema[i], descr[i], cls, verbose) for i in range(len(descr))])

            print_msg("Invalid types:")
            print_msg("type(dschema): {0}".format(type(dschema)))
            print_msg("type(descr): {0}".format(type(descr)))
            return False

        if not compatible_nestedmapping(dataloader_schema.inputs, self.inputs, ArraySchema, verbose):
            return False

        if (isinstance(dataloader_schema.targets, ArraySchema) or
            len(dataloader_schema.targets) > 0) and not compatible_nestedmapping(dataloader_schema.targets,
                                                                                 self.targets,
                                                                                 ArraySchema,
                                                                                 verbose):
            return False

        return True


# --------------------------------------------
# DataLoader specific components

@enum.unique
class MetadataType(enum.Enum):
    # TODO - make capital
    GENOMIC_RANGES = "GenomicRanges"
    STR = "str"
    INT = "int"
    FLOAT = "float"
    ARRAY = "array"
    # TODO - add bed3 or bed6 ranges


@related.immutable
class MetadataStruct(RelatedConfigMixin):

    doc = related.StringField()
    type = related.ChildField(MetadataType, required=False)
    name = related.StringField(required=False)

    def compatible_with_batch(self, batch, verbose=True):
        """Checks compatibility with a particular numpy array

        Args:
          batch: numpy array of a batch

          verbose: print the fail reason
        """

        def print_msg(msg):
            if verbose:
                print("MetadataStruct mismatch")
                print(msg)

        # custom classess
        if self.type == MetadataType.GENOMIC_RANGES:
            if not isinstance(batch, GenomicRanges):
                # TODO - do we strictly require the GenomicRanges class?
                #          - relates to metadata.py TODO about numpy_collate
                #        for now we should just be able to convert to the GenomicRanges class
                #        without any errors
                try:
                    GenomicRanges.from_dict(batch)
                except Exception as e:
                    print_msg("expecting a GenomicRanges object or a GenomicRanges-like dict")
                    print_msg("convertion error: {0}".format(e))
                    return False
                else:
                    return True
            else:
                return True

        # type = np.ndarray
        if not isinstance(batch, np.ndarray):
            print_msg("Expecting a np.ndarray. Got type(batch) = {0}".format(type(batch)))
            return False

        if not batch.ndim >= 1:
            print_msg("The array is a scalar (expecting at least the batch dimension)")
            return False

        bshape = batch.shape[1:]

        # scalars
        if self.type in {MetadataType.INT, MetadataType.STR, MetadataType.FLOAT}:
            if bshape != () and bshape != (1,):
                print_msg("expecting a scalar, got an array with shape (without the batch axis): {0}".format(bshape))
                return False

        # arrays
        # - no checks

        return True


@related.immutable
class DataLoaderSchema(RelatedConfigMixin):
    """Describes the model schema

    Properties:
     - we allow classes that contain also dictionaries
       -> leaf can be an
         - array
         - scalar
         - custom dictionary (recursive data-type)
         - SpecialType (say ArrayRanges, BatchArrayRanges, which will
                        effectively be a dicitonary of scalars)
    """
    inputs = NestedMappingField(ArraySchema, keyword="shape", key="name")
    targets = NestedMappingField(ArraySchema, keyword="shape", key="name", required=False)
    metadata = NestedMappingField(MetadataStruct, keyword="doc", key="name",
                                  required=False)

    def compatible_with_batch(self, batch, verbose=True):
        """Validate if the batch of data complies with the schema

        Checks preformed:
        - nested structure is the same (i.e. dictionary names, list length etc)
        - array shapes are compatible
        - returned obj classess are compatible

        # Arguments
            batch: a batch of data returned by one iteraton of dataloader's batch_iter
                nested dictionary
            verbose: verbose error logging if things don't match

        # Returns
           bool: True only if everyhing is ok
        """
        def print_msg(msg):
            if verbose:
                print(msg)

        # check the individual names
        if not isinstance(batch, dict):
            print("not isinstance(batch, dict)")
            return False

        # contains only the three specified fields
        if not set(batch.keys()).issubset({"inputs", "targets", "metadata"}):
            print('not set(batch.keys()).issubset({"inputs", "targets", "metadata"})')
            return False

        # Inputs check
        def compatible_nestedmapping(batch, descr, cls, verbose=True):
            """Recursive function of checks

            shapes match, batch-dim matches
            """
            # we expect a numpy array/special class, a list or a dictionary

            # Special case for the metadat
            if isinstance(descr, cls):
                return descr.compatible_with_batch(batch, verbose=verbose)
            elif isinstance(batch, collections.Mapping) and isinstance(descr, collections.Mapping):
                if not set(batch.keys()) == set(descr.keys()):
                    print_msg("The dictionary keys don't match:")
                    print_msg("batch: {0}".format(batch.keys()))
                    print_msg("descr: {0}".format(descr.keys()))
                    return False
                return all([compatible_nestedmapping(batch[key], descr[key], cls, verbose) for key in batch])
            elif isinstance(batch, collections.Sequence) and isinstance(descr, collections.Sequence):
                if not len(batch) == len(descr):
                    print_msg("Lengths dont match:")
                    print_msg("len(batch): {0}".format(len(batch)))
                    print_msg("len(descr): {0}".format(len(descr)))
                    return False
                return all([compatible_nestedmapping(batch[i], descr[i], cls, verbose) for i in range(len(batch))])

            print_msg("Invalid types:")
            print_msg("type(batch): {0}".format(type(batch)))
            print_msg("type(descr): {0}".format(type(descr)))
            return False

        # inputs needs to be present allways
        if "inputs" not in batch:
            print_msg('not "inputs" in batch')
            return False

        if not compatible_nestedmapping(batch["inputs"], self.inputs, ArraySchema, verbose):
            return False

        if "targets" in batch and not \
                (len(batch["targets"]) == 0):  # unspecified
            if self.targets is None:
                # targets need to be specified if we want to use them
                print_msg('self.targets is None')
                return False
            if not compatible_nestedmapping(batch["targets"], self.targets, ArraySchema, verbose):
                return False

        # metadata needs to be present if it is defined in the description
        if self.metadata is not None:
            if "metadata" not in batch:
                print_msg('not "metadata" in batch')
                return False
            if not compatible_nestedmapping(batch["metadata"], self.metadata, MetadataStruct, verbose):
                return False
        else:
            if "metadata" in batch:
                print_msg('"metadata" in batch')
                return False

        return True


@enum.unique
class PostProcType(enum.Enum):
    VAR_EFFECT_PREDICTION = "variant_effects"


@related.immutable
class PostProcSeqinput(object):
    seq_input = related.SequenceField(str)


@related.immutable
class PostProcStruct(RelatedConfigMixin):
    type = related.ChildField(PostProcType)  # enum
    args = related.ChildField(dict)  # contains


@related.immutable
class DataLoaderArgument(RelatedConfigMixin):
    # MAYBE - make this a general argument class
    doc = related.StringField()
    example = AnyField(required=False)
    name = related.StringField(required=False)
    type = related.StringField(default='str', required=False)
    optional = related.BooleanField(default=False, required=False)
    tags = StrSequenceField(str, default=[], required=False)  # TODO - restrict the tags


@related.immutable
class Dependencies(object):
    conda = related.SequenceField(str, default=[], required=False, repr=True)
    pip = related.SequenceField(str, default=[], required=False, repr=True)
    # not really required
    conda_channels = related.SequenceField(str, default=[], required=False, repr=True)

    def install_pip(self, dry_run=False):
        print("pip dependencies to be installed:")
        print(self.pip)
        if dry_run:
            return
        else:
            kconda.install_pip(self.pip)

    def install_conda(self, dry_run=False):
        print("Conda dependencies to be installed:")
        print(self.conda)
        if dry_run:
            return
        else:
            kconda.install_conda(self.conda)

    def install(self, dry_run=False):
        self.install_conda(dry_run)
        self.install_pip(dry_run)

    def merge(self, dependencies):
        """Merge one dependencies with another one

        Use case: merging the dependencies of model and dataloader

        Args:
          dependencies: Dependencies instance

        Returns:
          new Dependencies instance
        """
        return Dependencies(
            conda=unique_list(list(self.conda) + list(dependencies.conda)),
            pip=kconda.normalize_pip(list(self.pip) + list(dependencies.pip)),
            conda_channels=unique_list(list(self.conda_channels) + list(dependencies.conda_channels))
        )

    def to_env_dict(self, env_name):
        channels, packages = list(zip(*map(kconda.parse_conda_package, self.conda)))
        env_dict = OrderedDict(
            name=env_name,
            channels=unique_list(list(self.conda_channels) + list(channels)),
            dependencies=list(packages) + [OrderedDict(pip=kconda.normalize_pip(self.pip))]
        )
        return env_dict

    def to_env_file(self, env_name, path):
        """Dump the dependencies to a file
        """
        with open(path, 'w') as f:
            f.write(yaml_ordered_dump(self.to_env_dict(env_name),
                                      indent=4,
                                      default_flow_style=False))

    # @classmethod
    # def from_file(cls, path):
    #     """TODO instantiate Dependencies from a yaml file
    #     """
    #     pass


# --------------------------------------------
# Final description classes modelling the yaml files
@related.immutable
class ModelDescription(RelatedLoadSaveMixin):
    """Class representation of model.yaml
    """
    type = related.StringField()
    args = related.ChildField(dict)
    info = related.ChildField(Info)
    schema = related.ChildField(ModelSchema)
    default_dataloader = related.StringField(default='.')
    postprocessing = related.SequenceField(PostProcStruct, default=[], required=False)
    dependencies = related.ChildField(Dependencies,
                                      default=Dependencies(),
                                      required=False)
    path = related.StringField(required=False)
    # TODO - add after loading validation for the arguments class?


@related.immutable
class DataLoaderDescription(RelatedLoadSaveMixin):
    """Class representation of dataloader.yaml
    """
    type = related.StringField()
    defined_as = related.StringField()
    args = related.MappingField(DataLoaderArgument, "name")
    info = related.ChildField(Info)
    output_schema = related.ChildField(DataLoaderSchema)
    dependencies = related.ChildField(Dependencies, default=Dependencies(), required=False)
    path = related.StringField(required=False)
    postprocessing = related.SequenceField(PostProcStruct, default=[], required=False)


def example_kwargs(dl_args):
    """Return the example kwargs
    """
    return {k: v.example for k, v in six.iteritems(dl_args) if not isinstance(v.example, UNSPECIFIED)}

# TODO - special metadata classes should just extend the dictionary field
# (to be fully compatible with batching etc)
