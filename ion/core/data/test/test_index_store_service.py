#!/usr/bin/env python

"""
@file ion/services/dm/preservation/test/test_cassandra_manager_service.py
@author Matt Rodriguez
"""
  
import ion.util.ionlog
log = ion.util.ionlog.getLogger(__name__)

from twisted.internet import defer
from ion.test.iontest import IonTestCase

from ion.core.messaging.message_client import MessageClient
from ion.core.data.index_store_service import IndexStoreServiceClient
from ion.core.data.store import Query
from ion.core.object import object_utils

from ion.core import ioninit
CONF = ioninit.config(__name__)
from ion.util.itv_decorator import itv


resource_request_type = object_utils.create_type_identifier(object_id=10, version=1)
cassandra_indexed_row_type = object_utils.create_type_identifier(object_id=2511, version=1)

class IndexStoreServiceTester(IonTestCase):
    
    @itv(CONF) 
    @defer.inlineCallbacks
    def setUp(self):
        yield self._start_container()
        self.timeout = 30
        services = [
            {'name':'ds1','module':'ion.services.coi.datastore','class':'DataStoreService',
             'spawnargs':{'servicename':'datastore'}},
           {'name':'resource_registry1','module':'ion.services.coi.resource_registry_beta.resource_registry','class':'ResourceRegistryService',
             'spawnargs':{'datastore_service':'datastore'}},
             {'name': 'index_store_service',
             'module': 'ion.core.data.index_store_service',
             'class':'IndexStoreService',
             'spawnargs':{'indices':["Subject", "Predicate", "Object"]}}
        ]
        sup = yield self._spawn_processes(services)
        self.client = IndexStoreServiceClient(proc=sup)
        self.mc = MessageClient(proc = self.test_sup)
        
        
    @itv(CONF) 
    @defer.inlineCallbacks
    def tearDown(self):
        log.info("In tearDown")
        yield self._shutdown_processes()
        yield self._stop_container()
    
    @itv(CONF)  
    def test_instantiation_only(self):
        log.info("In test_instantiation_only")
        
    @itv(CONF)    
    @defer.inlineCallbacks
    def test_put(self):
        log.info("In test_put_rows")
        
        key = "Key1"
        value = "Value1"
        attr_dict = {"Subject":"Who", "Predicate":"Descriptive Verb", "Object": "The thing you're looking for"}
        put_response = yield self.client.put(key,value,attr_dict)   
    
     
    @itv(CONF)     
    @defer.inlineCallbacks
    def test_query(self): 
        key1 = "Key1"
        value1 = "Value1"
        attr_dict1 = {"Subject":"Me", "Predicate":"Descriptive Verb", "Object": "The first thing you're looking for"}
        key2 = "Key2"
        value2 = "Value2"
        attr_dict2 = {"Subject":"You", "Predicate":"Descriptive Verb", "Object": "The thing you're looking for"}
        key3 = "Key3"
        value3 = "Value3"
        attr_dict3 = {"Subject":"Me", "Predicate":"Descriptive Verb", "Object": "The second thing you're looking for"}
        put_response1 = yield self.client.put(key1, value1, attr_dict1)
        put_response2 = yield self.client.put(key2, value2, attr_dict2)
        put_response3 = yield self.client.put(key3, value3, attr_dict3)
        query_predicates = Query()
        query_predicates.add_predicate_eq("Subject", "Me")
        
        cassandra_rows = yield self.client.query(query_predicates)
        log.info(cassandra_rows)
        values = []
        for k,v in cassandra_rows.items():
            values.append(v["value"])

        correct_set = set(("Value1", "Value3"))    
        query_set = set(values)
        self.failUnlessEqual(correct_set, query_set)
    
    @itv(CONF)          
    @defer.inlineCallbacks
    def test_get(self):
        key = "Key1"
        value = "Value1"
        attr_dict = {"Subject":"Who", "Predicate":"Descriptive Verb", "Object": "The thing you're looking for"} 
        put_response = yield self.client.put(key,value,attr_dict)
        get_response = yield self.client.get(key)
        self.failUnlessEqual(get_response, value)
        
    @itv(CONF)      
    @defer.inlineCallbacks
    def test_remove(self):
        key = "Key1"
        value = "Value1"
        attr_dict = {"Subject":"Who", "Predicate":"Descriptive Verb", "Object": "The thing you're looking for"}
        put_response = yield self.client.put(key,value,attr_dict)  
        remove_response = yield self.client.remove(key)
        get_response = yield self.client.get(key)
        log.info(get_response)
        self.failUnlessEqual(get_response, None)
        
    @itv(CONF)      
    @defer.inlineCallbacks
    def test_get_query_attributes(self):
        index_attrs = yield self.client.get_query_attributes()
    
        correct_set = set(["Subject", "Predicate", "Object"])
        index_attrs_set = set(index_attrs)
        self.failUnlessEqual(correct_set, index_attrs_set)
        
