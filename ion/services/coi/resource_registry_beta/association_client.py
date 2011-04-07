#!/usr/bin/env python

"""
@file ion/services/coi/resource_registry_beta/association_client.py
@author David Stuebe
@brief Association Client and Association Instance are manager abstractions for associations

@ TODO
"""

from twisted.internet import defer

import ion.util.ionlog

log = ion.util.ionlog.getLogger(__name__)

from ion.core import ioninit

from ion.core.process import process
from ion.core.object import workbench
from ion.core.object.association_manager import AssociationInstance, AssociationManager

from ion.services.dm.inventory.association_service import AssociationServiceClient
from ion.services.coi.datastore_bootstrap import ion_preload_config

from ion.services.coi.datastore_bootstrap.ion_preload_config import OWNED_BY_ID

from google.protobuf import message
from google.protobuf.internal import containers
from ion.core.object import object_utils


RESOURCE_DESCRIPTION_TYPE = object_utils.create_type_identifier(object_id=1101, version=1)
RESOURCE_TYPE = object_utils.create_type_identifier(object_id=1102, version=1)
IDREF_TYPE = object_utils.create_type_identifier(object_id=4, version=1)

CONF = ioninit.config(__name__)

class AssociationClientError(Exception):
    """
    A class for association client exceptions
    """


class AssociationClient(object):
    """
    @brief This is the base class for a resource client. It is a factory for resource
    instances. The resource instance provides the interface for working with resources.
    The client helps create and manage resource instances.
    """

    def __init__(self, proc=None, datastore_service='datastore'):
        """
        Initializes a association client
        @param proc a IProcess instance as originator of messages
        @param datastore the name of the datastore service with which you wish to
        interact with the OOICI.
        """
        if not proc:
            proc = process.Process()

        self.proc = proc

        # The resource client is backed by a process workbench.
        self.workbench = self.proc.workbench

        self.datastore_service = datastore_service

        self.asc = AssociationServiceClient(proc=self)



    @defer.inlineCallbacks
    def _check_init(self):
        """
        Called in client methods to ensure that there exists a spawned process
        to send and receive messages
        """
        if not self.proc.is_spawned():
            yield self.proc.spawn()

        assert isinstance(self.workbench, workbench.WorkBench),\
        'Process workbench is not initialized'




    @defer.inlineCallbacks
    def create_association(self, subject, predicate_id, obj):
        """
        @Brief Create an association between two resource instances
        @param subject is a resource instance which is to be the subject of the association
        @param predicate_id is the predicate id to use in creating the association
        @param obj is a resource instance which is to be the object of the association
        """
        yield self._check_init()

        #if not isinstance(ResourceInstance, subject):
        #    raise TypeError('The subject argument in the resource client, create_association method must be a resource instance.')
        #
        #if not isinstance(ResourceInstance, obj):
        #    raise TypeError('The obj argument in the resource client, create_association method must be a resource instance.')

        yield self.workbench.pull(self.datastore_service, predicate_id)
        predicate_repo = self.workbench.get_repository(predicate_id)
        yield predicate_repo.checkout('master')

        # The workbench method returns a fully formed association instance!
        association = self.workbench.create_association(subject, predicate_repo, obj)

        defer.returnValue(association)

    @defer.inlineCallbacks
    def get_instance(self, association_id):
        """
        @brief Get the latest version of the identified association from the data store
        @param association_id can be either a string association identity or an IDRef
        object which specifies the association identity as well as optional parameters
        version and version state.
        @retval the specified AssociationInstance

        """
        yield self._check_init()

        reference = None
        branch = 'master'
        commit = None

        # Get the type of the argument and act accordingly
        if hasattr(association_id, 'ObjectType') and association_id.ObjectType == IDREF_TYPE:
            # If it is a resource reference, unpack it.
            if association_id.branch:
                branch = association_id.branch

            reference = association_id.key
            commit = association_id.commit

        elif isinstance(association_id, (str, unicode)):
            # if it is a string, us it as an identity
            reference = association_id
            # @TODO Some reasonable test to make sure it is valid?

        else:
            raise AssociationClientError('''Illegal argument type in get_instance:
                                      \n type: %s \nvalue: %s''' % (type(association_id), str(association_id)))

            # Pull the repository
        try:
            result = yield self.workbench.pull(self.datastore_service, reference)
        except workbench.WorkBenchError, ex:
            log.warn(ex)
            raise AssociationClientError('Could not pull the requested association from the datastore. Workbench exception: \n %s' % ex)

        # Get the repository
        repo = self.workbench.get_repository(reference)
        try:
            yield repo.checkout(branch)
        except repository.RepositoryError, ex:
            log.warn('Could not check out branch "%s":\n Current repo state:\n %s' % (branch, str(repo)))
            raise ResourceClientError('Could not checkout branch during get_instance.')

        # Create a association instance to return
        # @TODO - Check and see if there is already one - what to do?
        association = AssociationInstance(repo)

        defer.returnValue(association)

    @defer.inlineCallbacks
    def association_exists(self, subject_id, predicate_id, object_id):
        """
        @Brief Test for the existence of an association between these three resource or object identities
        @TODO change to take either string or IDref 
        """

        request = yield self.proc.message_client.create_instance(ASSOCIATION_QUERY_MSG_TYPE)

        request.object = request.CreateObject(IDREF_TYPE)
        request.object.key = subject_id

        request.predicate = request.CreateObject(IDREF_TYPE)
        request.predicate.key = predicate_id

        request.subject = request.CreateObject(IDREF_TYPE)
        request.subject.key = object_id


        result = yield self.asc.association_exists(request)

        defer.returnValue(result.result)


    @defer.inlineCallbacks
    def find_associations(self, subject=None, obj=None, predicate_or_predicates=None):
        """
        @Brief Get association to a resource instances as either subject or object. Specify a predicate or predicates to limit the results
        """

        predicates = predicate_or_predicates
        if predicates is None:
            predicates = [None]
            
        else:
            if None in predicates:
                raise AssociationClientError('None can not be in the list of predicates passed to get_associations')

        if subject is None and obj is None:
            raise AssociationClientError('Either the subject and/or the obj must be specified in get associations')


        if subject is not None and not isinstance(subject, ResourceInstance):
            raise AssociationClientError('The subject argument in the resource client, get_associations method must be a resource instance.')

        if obj is not None and not isinstance(obj, ResourceInstance):
            raise AssociationClientError('The "obj" argument in the resource client, get_associations method must be a resource instance.')


        def_list = []

        for predicate in predicates:

            request = yield self.proc.message_client.create_instance(ASSOCIATION_QUERY_MSG_TYPE)

            if obj is not None:
                request.object = request.CreateObject(IDREF_TYPE)
                request.object.key = ANONYMOUS_USER_ID

            if predicate is not None:
                request.predicate = request.CreateObject(IDREF_TYPE)
                request.predicate.key = OWNED_BY_ID

            if subject is not None:
                request.subject = request.CreateObject(IDREF_TYPE)
                request.subject.key = ROOT_USER_ID

            def_list.append(self.asc.get_associations(request))


        result_list = yield defer.DeferredList(def_list)

        association_manager = AssociationManager()
        for result, assoc_ref_list in result_list:

            for assoc_ref in assoc_ref_list.idrefs:

                yield self.workbench.pull(self.datastore_service, assoc_ref.key)
                assoc = self.workbench.get_repository(assoc_ref.key)
                assoc.checkout(assoc_ref.branch)

                association = AssociationInstance(assoc, self.workbench)

                association_manager.add(association)

        defer.returnValue(association_manager)



