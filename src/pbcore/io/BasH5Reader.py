#################################################################################
# Copyright (c) 2011-2013, Pacific Biosciences of California, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
# * Neither the name of Pacific Biosciences nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE.  THIS SOFTWARE IS PROVIDED BY PACIFIC BIOSCIENCES AND ITS
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL PACIFIC BIOSCIENCES OR
# ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#################################################################################

# Authors: David Alexander, Jim Bullard

__all__ = [ "BasH5Reader" ,
            "BaxH5Reader" ]

import h5py, numpy as np, os.path as op
from bisect import bisect_left, bisect_right
from operator import getitem
from ._utils import arrayFromDataset

def intersectRanges(r1, r2):
    b1, e1 = r1
    b2, e2 = r2
    b, e = max(b1, b2), min(e1, e2)
    return (b, e) if (b < e) else None

def rangeLength(r):
    b, e = r
    return e - b

def removeNones(lst):
    return filter(lambda x: x!=None, lst)

# ZMW hole Types
SEQUENCING_ZMW = 0

# Region types
ADAPTER_REGION = 0
INSERT_REGION  = 1
HQ_REGION      = 2

# This seems to be the magic incantation to get a RecArray that can be
# indexed to yield a record that can then be accessed using dot
# notation.
def toRecArray(dtype, arr):
    return np.rec.array(arr, dtype=dtype).flatten()

REGION_TABLE_DTYPE = [("holeNumber",  np.int32),
                      ("regionType",  np.int32),
                      ("regionStart", np.int32),
                      ("regionEnd",   np.int32),
                      ("regionScore", np.int32) ]

def _makeQvAccessor(featureName):
    def f(self):
        return self.qv(featureName)
    return f

class Zmw(object):
    """
    A Zmw represents all data from a ZMW (zero-mode waveguide) hole
    within a bas.h5 movie file.  Accessor methods provide convenient
    access to the read (or subreads), and to the region table entries
    for this hole.
    """
    __slots__ = [ "baxH5", "holeNumber", "index"]

    def __init__(self, baxH5, holeNumber):
        self.baxH5               = baxH5
        self.holeNumber          = holeNumber
        self.index               = self.baxH5._holeNumberToIndex[holeNumber]

    @property
    def regionTable(self):
        startRow, endRow = self.baxH5._regionTableIndex[self.holeNumber]
        return self.baxH5.regionTable[startRow:endRow]

    #
    # The following calls return one or more intervals ( (int, int) ).
    # All intervals are clipped to the hqRegion.
    #
    @property
    def adapterRegions(self):
        unclippedAdapterRegions = \
           [ (region.regionStart, region.regionEnd)
             for region in self.regionTable
             if region.regionType == ADAPTER_REGION ]
        hqRegion = self.hqRegion
        return removeNones([ intersectRanges(hqRegion, region)
                             for region in unclippedAdapterRegions ])

    @property
    def insertRegions(self):
        unclippedInsertRegions = \
           [ (region.regionStart, region.regionEnd)
             for region in self.regionTable
             if region.regionType == INSERT_REGION ]
        hqRegion = self.hqRegion
        return removeNones([ intersectRanges(hqRegion, region)
                             for region in unclippedInsertRegions ])
    @property
    def hqRegion(self):
        rt = self.regionTable
        hqRows = rt[rt.regionType == HQ_REGION]
        if len(hqRows) == 1:
            hqRow = hqRows[0]
            return hqRow.regionStart, hqRow.regionEnd
        else:
            # Broken region table, bug 23585
            return 0, 0

    @property
    def readScore(self):
        """
        Return the "read score", a prediction of the accuracy (between 0 and 1) of the
        basecalls from this ZMW, from the `ReadScore` dataset in the
        file
        """
        return self.baxH5._readScores[self.index]

    @property
    def productivity(self):
        """
        Return the 'productivity' of this ZMW, which is the estimated
        number of polymerase reactions taking place within it.  For
        example, a doubly-loaded ZMW would have productivity 2.
        """
        return self.baxH5._productivities[self.index]


    def zmwMetric(self, name):
        """
        Return the value of metric 'name' from the ZMW metrics.
        """
        return self.baxH5.zmwMetric(name, self.index)

    def listZmwMetrics(self):
        """
        List the available ZMW metrics for this bax.h5 file.
        """
        return self.baxH5.listZmwMetrics()

    @property
    def numPasses(self):
        """
        Return the number of passes (forward + back) across the SMRTbell
        insert, used to forming the CCS consensus.
        """
        if not self.baxH5.hasConsensusBasecalls:
            raise ValueError, "No CCS reads in this file"
        return self.baxH5._ccsNumPasses[self.index]

    #
    # The following calls return one or more ZmwRead objects.
    #
    def read(self, readStart=None, readEnd=None):
        if not self.baxH5.hasRawBasecalls:
            raise ValueError, "No raw reads in this file"
        hqStart, hqEnd = self.hqRegion
        readStart = hqStart if readStart is None else readStart
        readEnd   = hqEnd if readEnd is None else readEnd
        return ZmwRead(self.baxH5, self.holeNumber, readStart, readEnd)

    @property
    def subreads(self):
        if not self.baxH5.hasRawBasecalls:
            raise ValueError, "No raw reads in this file"
        return [ self.read(readStart, readEnd)
                 for (readStart, readEnd) in self.insertRegions ]

    @property
    def adapters(self):
        if not self.baxH5.hasRawBasecalls:
            raise ValueError, "No raw reads in this file"
        return [ self.read(readStart, readEnd)
                 for (readStart, readEnd) in self.adapterRegions ]
    @property
    def ccsRead(self):
        if not self.baxH5.hasConsensusBasecalls:
            raise ValueError, "No CCS reads in this file"
        baseOffset  = self.baxH5._ccsOffsetsByHole[self.holeNumber]
        if (baseOffset[1] - baseOffset[0]) <= 0:
            return None
        else:
            return CCSZmwRead(self.baxH5, self.holeNumber, 0,
                              baseOffset[1] - baseOffset[0])

    def __repr__(self):
        zmwName = "%s/%d" % (self.baxH5.movieName,
                             self.holeNumber)
        return "<Zmw: %s>" % zmwName


class ZmwRead(object):
    """
    A ZmwRead represents the data features (basecalls as well as pulse
    features) recorded from the ZMW, delimited by readStart and readEnd.
    """
    __slots__ = [ "baxH5", "holeNumber",
                  "readStart", "readEnd",
                  "offsetBegin", "offsetEnd" ]

    def __init__(self, baxH5, holeNumber, readStart, readEnd):
        self.baxH5        = baxH5
        self.holeNumber   = holeNumber
        self.readStart    = readStart
        self.readEnd      = readEnd
        zmwOffsetBegin, zmwOffsetEnd = self._getOffsets()[self.holeNumber]
        self.offsetBegin = zmwOffsetBegin + self.readStart
        self.offsetEnd   = zmwOffsetBegin + self.readEnd
        if not (zmwOffsetBegin   <=
                self.offsetBegin <=
                self.offsetEnd   <=
                zmwOffsetEnd):
            raise IndexError, "Invalid slice of Zmw!"

    def _getBasecallsGroup(self):
        return self.baxH5._basecallsGroup

    def _getOffsets(self):
        return self.baxH5._offsetsByHole

    @property
    def zmw(self):
        return self.baxH5[self.holeNumber]

    @property
    def readName(self):
        return "%s/%d/%d_%d" % (self.baxH5.movieName,
                                self.holeNumber,
                                self.readStart,
                                self.readEnd)

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__,
                             self.readName)

    def __len__(self):
        return self.readEnd - self.readStart

    def basecalls(self):
        return arrayFromDataset(self._getBasecallsGroup()["Basecall"],
                                self.offsetBegin, self.offsetEnd).tostring()

    def qv(self, qvName):
        return arrayFromDataset(self._getBasecallsGroup()[qvName],
                                self.offsetBegin, self.offsetEnd)

    PreBaseFrames  = _makeQvAccessor("PreBaseFrames")
    IPD            = _makeQvAccessor("PreBaseFrames")

    WidthInFrames  = _makeQvAccessor("WidthInFrames")
    PulseWidth     = _makeQvAccessor("WidthInFrames")

    QualityValue   = _makeQvAccessor("QualityValue")
    InsertionQV    = _makeQvAccessor("InsertionQV")
    DeletionQV     = _makeQvAccessor("DeletionQV")
    DeletionTag    = _makeQvAccessor("DeletionTag")
    MergeQV        = _makeQvAccessor("MergeQV")
    SubstitutionQV = _makeQvAccessor("SubstitutionQV")
    SubstitutionTag = _makeQvAccessor("SubstitutionTag")


class CCSZmwRead(ZmwRead):
    """
    Class providing access to the CCS (circular consensus sequencing)
    data calculated for a ZMW.
    """
    def _getBasecallsGroup(self):
        return self.baxH5._ccsBasecallsGroup

    def _getOffsets(self):
        return self.baxH5._ccsOffsetsByHole

    @property
    def readName(self):
        return "%s/%d/ccs" % (self.baxH5.movieName, self.holeNumber)

def _makeOffsetsDataStructure(h5Group):
    numEvent   = h5Group["ZMW/NumEvent"].value
    holeNumber = h5Group["ZMW/HoleNumber"].value
    endOffset = np.cumsum(numEvent)
    beginOffset = np.hstack(([0], endOffset[0:-1]))
    offsets = zip(beginOffset, endOffset)
    return dict(zip(holeNumber, offsets))

def _makeRegionTableIndex(regionTableHoleNumbers):
    #  returns a dict: holeNumber -> (startRow, endRow)
    diffs = np.ediff1d(regionTableHoleNumbers,
                       to_begin=[1], to_end=[1])
    changepoints = np.flatnonzero(diffs)
    startsAndEnds = zip(changepoints[:-1],
                        changepoints[1:])
    return dict(zip(np.unique(regionTableHoleNumbers),
                    startsAndEnds))

class BaxH5Reader(object):
    """
    The `BaxH5Reader` class provides access to bax.h5 file and
    single-part bas.h5 files.
    """
    def __init__(self, filename):
        self.filename = op.abspath(op.expanduser(filename))
        self.file = h5py.File(self.filename, "r")
        #
        # Raw base calls?
        #
        if "BaseCalls" in self.file["/PulseData"]:
            self._basecallsGroup = self.file["/PulseData/BaseCalls"]
            self._offsetsByHole  = _makeOffsetsDataStructure(self._basecallsGroup)
            self.hasRawBasecalls = True
        else:
            self.hasRawBasecalls = False
        #
        # CCS base calls?
        #
        if "ConsensusBaseCalls" in self.file["/PulseData"].keys():
            self._ccsBasecallsGroup = self.file["/PulseData/ConsensusBaseCalls"]
            self._ccsOffsetsByHole  = _makeOffsetsDataStructure(self._ccsBasecallsGroup)
            self._ccsNumPasses      = self._ccsBasecallsGroup["Passes/NumPasses"]
            self.hasConsensusBasecalls = True
        else:
            self.hasConsensusBasecalls = False

        self._mainBasecallsGroup = self._basecallsGroup if self.hasRawBasecalls \
                                   else self._ccsBasecallsGroup

        self._readScores     = self._mainBasecallsGroup["ZMWMetrics/ReadScore"].value
        self._productivities = self._mainBasecallsGroup["ZMWMetrics/Productivity"].value

        holeNumbers = self._mainBasecallsGroup["ZMW/HoleNumber"].value
        self._holeNumberToIndex = dict(zip(holeNumbers, range(len(holeNumbers))))

        #
        # Region table
        #
        self.regionTable = toRecArray(REGION_TABLE_DTYPE,
                                      self.file["/PulseData/Regions"].value)
        self._regionTableIndex = _makeRegionTableIndex(self.regionTable.holeNumber)
        isHqRegion     = self.regionTable.regionType == HQ_REGION
        hqRegions      = self.regionTable[isHqRegion, :]

        if len(hqRegions) != len(holeNumbers):
            # Bug 23585: pre-2.1 primary had a bug where a bas file
            # could get a broken region table, lacking an HQ region
            # entry for a ZMW.  This happened fairly rarely, mostly on
            # very long traces.  Workaround here is to rebuild HQ
            # regions table with empty HQ region entries for those
            # ZMWs.
            hqRegions_ = toRecArray(REGION_TABLE_DTYPE,
                                    np.zeros(shape=len(holeNumbers),
                                             dtype=REGION_TABLE_DTYPE))
            hqRegions_.holeNumber = holeNumbers
            for record in hqRegions:
                hn = record.holeNumber
                hqRegions_[self._holeNumberToIndex[hn]] = record
            hqRegions = hqRegions_

        hqRegionLength = hqRegions.regionEnd - hqRegions.regionStart
        holeStatus     = self._mainBasecallsGroup["ZMW/HoleStatus"].value

        #
        # Sequencing ZMWs - Note: this differs from Primary's
        # definition. To obtain those values, one would use the
        # `allSequencingZmws` property.
        #
        self._sequencingZmws = \
            holeNumbers[(holeStatus == SEQUENCING_ZMW)                  &
                        (self._mainBasecallsGroup["ZMW/NumEvent"] >  0) &
                        (hqRegionLength >  0)]

        #
        # ZMW metric cache -- probably want to move prod and readScore
        # here.
        # 
        self.__metricCache = {}

    @property
    def sequencingZmws(self):
        """
        A list of the hole numbers that produced useable sequence data
        """
        return self._sequencingZmws

    @property
    def allSequencingZmws(self):
        """
        A list of the hole numbers that are capable of producing
        sequencing data. This differs from the `sequencingZmws` in
        that zmws are not filtered according to their HQ status. This
        number is fixed per chip, whereas the `sequencingZmws` depends
        on things such as loading.
        """
        hStatus = self._mainBasecallsGroup["ZMW/HoleStatus"].value
        hNumber = self._mainBasecallsGroup["ZMW/HoleNumber"].value
        return hNumber[hStatus == SEQUENCING_ZMW]

    def __getitem__(self, holeNumber):
        return Zmw(self, holeNumber)

    @property
    def movieName(self):
        return self.file["/ScanData/RunInfo"].attrs["MovieName"]

    def __len__(self):
        return len(self.sequencingZmws)

    def close(self):
        if hasattr(self, "file") and self.file != None:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __iter__(self):
        for holeNumber in self.sequencingZmws:
            yield self[holeNumber]

    def listZmwMetrics(self):
        return self._basecallsGroup["ZMWMetrics"].keys()

    def zmwMetric(self, name, index):
        # we are going to cache these lazily because it is very likely
        # that if one ZMW asked for the metric others aren't far
        # behind.
        if name not in self.__metricCache:
            k = "/".join(("ZMWMetrics", name))
            self.__metricCache[name] = self._mainBasecallsGroup[k].value

        v = self.__metricCache[name]
        if len(v.shape) > 1:
            return v[index,]
        else:
            return v[index]


class BasH5Reader(object):
    """
    The `BasH5Reader` provides access to the basecall and pulse metric
    data encoded in PacBio bas.h5 files.  To access data using a
    `BasH5Reader`, the standard idiom is:

    1. Index into the `BasH5Reader` using the ZMW hole number to get a `Zmw` object::

       >>> from pbcore.io import BasH5Reader
       >>> b = BasH5Reader("myMovie.bas.h5")
       >>> zmw8 = b[8]

    2. Extract `ZmwRead` objects from the `Zmw` object by:

       - Using the `.subreads` property to extract the subreads, which
         are the subintervals of the raw read corresponding to the
         SMRTbell insert::

           >>> print zmw8.subreads
           [<ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/3381_3881>,
            <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/3924_4398>,
            <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/4445_4873>,
            <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/4920_5354>,
            <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/5413_5495>]

       - Using the `.ccsRead` property to extract the CCS (consensus)
         read, which is a consensus sequence precomputed from the
         subreads.  Note that CCS data is not available for every
         sequencing ZMW hole, for example some holes have too few
         subreads for the computation of a consensus::

           >>> zmw8.ccsRead
           <CCSZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/ccs>

       - Use the `.read()` method to get the full raw read, or
         `.read(start, end)` to extract a custom subinterval.

           >>> zmw8.read()
           <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/3381_5495>
           >>> zmw8.read(3390, 3400)
           <ZmwRead: m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/3390_3400>

    3. With a `ZmwRead` object in hand, extract the desired
       basecalls and pulse metrics::

         >>> subreads[0].readName
         "m110818_075520_42141_c100129202555500000315043109121112_s1_p0/8/3381_3881"
         >>> subreads[0].basecalls()
         "AGCCCCGTCGAGAACATACAGGTGGCCAATTTCACAGCCTCTTGCCTGGGCGATCCCGAACATCGCACCGGA..."
         >>> subreads[0].InsertionQV()
         array([12, 12, 10,  2,  7, 14, 13, 18, 15, 16, 16, 15, 10, 12,  3, 14, ...])

    Note that not every ZMW on a chip produces useable sequencing
    data.  The `BasH5Reader` has a propery `sequencingZmws` is a list
    of the hole numbers where useable sequence was recorded.
    Iteration over the `BasH5Reader` object allows you to iterate over
    the `Zmw` objects providing useable sequence.
    """
    def __init__(self, filename):
        self.filename = op.abspath(op.expanduser(filename))
        self.file = h5py.File(self.filename, "r")
        # Is this a multi-part or single-part?
        if self.file.get("MultiPart"):
            directory = op.dirname(self.filename)
            self._parts = [ BaxH5Reader(op.join(directory, fn))
                            for fn in self.file["/MultiPart/Parts"] ]
            self._holeLookupVector = self.file["/MultiPart/HoleLookup"][:,1]
            self._holeLookup = self._holeLookupVector.__getitem__
        else:
            self._parts = [ BaxH5Reader(self.filename) ]
            self._holeLookup = (lambda holeNumber: 1)
        self._sequencingZmws = np.concatenate([ part.sequencingZmws
                                                for part in self._parts ])

    @property
    def parts(self):
        return self._parts

    @property
    def sequencingZmws(self):
        return self._sequencingZmws

    @property
    def allSequencingZmws(self):
        return np.concatenate([ part.allSequencingZmws
                                for part in self._parts ])

    @property
    def hasConsensusBasecalls(self):
        return all(part.hasConsensusBasecalls for part in self._parts)

    @property
    def hasRawBasecalls(self):
        return all(part.hasRawBasecalls for part in self._parts)

    def __iter__(self):
        for holeNumber in self.sequencingZmws:
            yield self[holeNumber]

    def __len__(self):
        return len(self.sequencingZmws)

    def _getitemScalar(self, holeNumber):
        part = self.parts[self._holeLookup(holeNumber)-1]
        return part[holeNumber]

    def __getitem__(self, holeNumbers):
        if (isinstance(holeNumbers, int) or
            issubclass(type(holeNumbers), np.integer)):
            return self._getitemScalar(holeNumbers)
        elif isinstance(holeNumbers, slice):
            return [ self._getitemScalar(r)
                     for r in xrange(*holeNumbers.indices(len(self)))]
        elif isinstance(holeNumbers, list) or isinstance(holeNumbers, np.ndarray):
            if len(holeNumbers) == 0:
                return []
            else:
                entryType = type(holeNumbers[0])
                if entryType == int or issubclass(entryType, np.integer):
                    return [ self._getitemScalar(r) for r in holeNumbers ]
                elif entryType == bool or issubclass(entryType, np.bool_):
                    return [ self._getitemScalar(r) for r in np.flatnonzero(holeNumbers) ]
        raise TypeError, "Invalid type for BasH5Reader slicing"

    @property
    def movieName(self):
        return self._parts[0].movieName

    def __len__(self):
        return len(self.sequencingZmws)

    def close(self):
        if hasattr(self, "file") and self.file != None:
            self.file.close()
            self.file = None
        for part in self.parts:
            part.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __iter__(self):
        for holeNumber in self.sequencingZmws:
            yield self[holeNumber]

    def __repr__(self):
        return "<BasH5Reader: %s>" % op.basename(self.filename)


    # Make cursor classes available
    Zmw        = Zmw
    ZmwRead    = ZmwRead
    CCSZmwRead = CCSZmwRead
