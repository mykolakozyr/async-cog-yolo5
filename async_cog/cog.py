from __future__ import annotations

from struct import calcsize, pack, unpack
from typing import Any, Iterator, List, Literal

from aiohttp import ClientSession

from async_cog.ifd import IFD, Tag


class COGReader:
    _version: int
    _first_ifd_pointer: int
    _ifds: List[IFD]
    # For characters meainng in *_fmt
    # https://docs.python.org/3.10/library/struct.html#format-characters
    _byte_order_fmt: Literal["<", ">"]
    _pointer_fmt: Literal["I", "Q"]
    _n_fmt: Literal["H", "Q"]

    def __init__(self, url: str):
        self._url: str = url
        self._ifds = []

    async def __aenter__(self) -> COGReader:
        self._client = ClientSession()

        try:
            await self._read_header()
            await self._read_idfs()
        except AssertionError:
            raise ValueError("Invalid file format")

        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        await self._client.close()

    @property
    def _tag_format(self) -> str:
        return self._format(f"HH2{self._pointer_fmt}")

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_bigtiff(self) -> bool:
        return self._version == 43

    def _format(self, format_str: str) -> str:
        "Adds byte-order endian to struct format string"
        return f"{self._byte_order_fmt}{format_str}"

    async def _read(self, offset: int, size: int) -> bytes:
        header = {"Range": f"bytes={offset}-{offset + size - 1}"}

        async with self._client.get(self.url, headers=header) as response:
            assert response.ok
            return await response.read()

    async def _read_header(self) -> None:
        """
        Reads TIFF header. See functions docstrings to get it's structure
        """
        await self._read_first_header()

        if self.is_bigtiff:
            self._pointer_fmt = "Q"
            self._n_fmt = "Q"

            await self._read_bigtiff_second_header()
        else:
            self._pointer_fmt = "I"
            self._n_fmt = "H"

            await self._read_second_header()

    async def _read_idfs(self) -> None:
        pointer = self._first_ifd_pointer

        while pointer > 0:
            ifd = await self._read_ifd(pointer)
            self._ifds.append(ifd)
            pointer = ifd.next_ifd_pointer

    async def _read_first_header(self) -> None:
        """
        First header structure

        +------+-----+------------------------------------------------+
        |offset| size|                                           value|
        +------+-----+------------------------------------------------+
        |     0|    2|               "II" for little-endian byte order|
        |      |     |               "MM" for big-endian byte order   |
        +------+-----+------------------------------------------------+
        |     2|    2| Version number (42 for TIFF and 43 for BigTIFF)|
        +------+-----+------------------------------------------------+
        """
        OFFSET = 0

        data = await self._read(OFFSET, 4)

        # Read first two bytes and skip the last two
        (first_bytest,) = unpack("2s2x", data)

        # https://docs.python.org/3.8/library/struct.html#byte-order-size-and-alignment
        if first_bytest == b"II":
            self._byte_order_fmt = "<"
        elif first_bytest == b"MM":
            self._byte_order_fmt = ">"
        else:
            raise AssertionError

        # Skip first two bytes and read the last two as SHORT
        (self._version,) = unpack(self._format("2xH"), data)

        assert self._version in (42, 43)

    async def _read_second_header(self) -> None:
        """
        Second header structure for TIFF

        +------+-----+---------------------+
        |offset| size|                value|
        +------+-----+---------------------+
        |     4|    4| Pointer to first IFD|
        +------+-----+---------------------+
        """
        OFFSET = 4
        format_str = self._format(self._pointer_fmt)

        data = await self._read(OFFSET, calcsize(format_str))
        (self._first_ifd_pointer,) = unpack(format_str, data)

    async def _read_bigtiff_second_header(self) -> None:
        """
        Second header structure for BigTIFF

        +------+-----+----------------------------------------------+
        |offset| size|                                         value|
        +------+-----+----------------------------------------------+
        |     4|    2| Bytesize of IFD pointers (should always be 8)|
        +------+-----+----------------------------------------------+
        |     6|    2|                                      Always 0|
        +------+-----+----------------------------------------------+
        |     8|    8|                          Pointer to first IFD|
        +------+-----+----------------------------------------------+
        """
        OFFSET = 4
        format_str = self._format("HHQ")

        data = await self._read(OFFSET, calcsize(format_str))

        bytesize, placeholder, self._first_ifd_pointer = unpack(format_str, data)

        assert bytesize == 8
        assert placeholder == 0

    async def _read_ifd(self, ifd_pointer: int) -> IFD:
        """
        IFD structure (all offsets are relative to ifd_offset):
        +------------+------------+------------------------------------------+
        |      offset|        size|                                     value|
        +------------+------------+------------------------------------------+
        |           0|  ifd_n_size|                 n — number of tags in IFD|
        +------------+------------+------------------------------------------+
        |           2|    tag_size|                                Tag 0 data|
        +------------+------------+------------------------------------------+
        |         ...|         ...|                                       ...|
        +------------+------------+------------------------------------------+
        |2+x*tag_size|    tag_size|                                     Tag x|
        +------------+------------+------------------------------------------+
        |         ...|         ...|                                       ...|
        +------------+------------+------------------------------------------+
        |2+n*tag_size|pointer_size|                      Pointer to next IFD |
        +------------+------------+------------------------------------------+
        """
        n_format_str = self._format(self._n_fmt)

        # Read nubmer of tags in the IFD
        n_data = await self._read(ifd_pointer, calcsize(n_format_str))
        (n_tags,) = unpack(n_format_str, n_data)

        tags_len = n_tags * calcsize(self._tag_format)
        ifd_offset = ifd_pointer + calcsize(n_format_str)
        ifd_format_str = self._format(f"{tags_len}s{self._pointer_fmt}")

        # Read tags data and pointer to next IFD
        ifd_data = await self._read(ifd_offset, calcsize(ifd_format_str))

        tags_data, pointer = unpack(ifd_format_str, ifd_data)
        tags = self._tags_from_data(n_tags, tags_data)

        return IFD(
            offset=ifd_pointer,
            n_tags=n_tags,
            next_ifd_pointer=pointer,
            tags=tags,
        )

    def _tags_from_data(self, n_tags: int, tags_data: bytes) -> Iterator[Tag]:
        """
        Split data into tag-sized buffers and parse them
        """
        size = calcsize(self._tag_format)

        # Split tag_data into n tag-sized chuncks
        for tag_data in unpack(n_tags * f"{size}s", tags_data):
            try:
                tag = self._tag_from_tag_data(tag_data)
            except ValueError:
                continue

            yield tag

    def _tag_from_tag_data(self, tag_data: bytes) -> Tag:
        """
        Tag structure

        +--------------+------------+-----------------------------------+
        |        offset|        size|                              value|
        +--------------+------------+-----------------------------------+
        |             0|           2|       Tag code (see ifd.TAG_NAMES)|
        +--------------+------------+-----------------------------------+
        |             2|           2|  Tag data type (see ifd.TAG_TYPES)|
        +--------------+------------+-----------------------------------+
        |             4|pointer_size|                   Number of values|
        +--------------+------------+-----------------------------------+
        |4+pointer_size|pointer_size| Pointer to the data or data itself|
        |              |            |       if it's size <= pointer_size|
        +--------------+------------+-----------------------------------+
        """
        code, tag_type, n_values, pointer = unpack(self._tag_format, tag_data)

        tag = Tag(code=code, type=tag_type, n_values=n_values, pointer=pointer)

        # If tag data type fits into it's pointer size, then last bytes contain
        # data, not it's pointer
        if tag.data_size <= calcsize(self._pointer_fmt):
            tag.data = pack(self._pointer_fmt, pointer)[: tag.data_size]
            tag.pointer = None

        return tag