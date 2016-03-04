import struct
from socket import htons
from common import stats2
from common import nla_plus_police
from common import nla_plus_tca_act_opt
from common import get_tca_action
from common import get_filter_police_parameter
from common import TCA_ACT_MAX_PRIO
from pyroute2.netlink import nla
from pyroute2.netlink import nlmsg


def fix_msg(msg, kwarg):
    msg['info'] = htons(kwarg.get('protocol', 0) & 0xffff) |\
            ((kwarg.get('prio', 0) << 16) & 0xffff0000)


def get_parameters(kwarg):
    ret = {'attrs': []}

    if kwarg.get('rate'):
        ret['attrs'].append([
            'TCA_U32_POLICE',
            {'attrs': get_filter_police_parameter(kwarg)}
        ])
    elif kwarg.get('action'):
        ret['attrs'].append(['TCA_U32_ACT', get_tca_action(kwarg)])

    ret['attrs'].append(['TCA_U32_CLASSID', kwarg['target']])
    ret['attrs'].append(['TCA_U32_SEL', {'keys': kwarg['keys']}])

    return ret


class options(nla, nla_plus_police):
    nla_map = (('TCA_U32_UNSPEC', 'none'),
               ('TCA_U32_CLASSID', 'uint32'),
               ('TCA_U32_HASH', 'uint32'),
               ('TCA_U32_LINK', 'hex'),
               ('TCA_U32_DIVISOR', 'uint32'),
               ('TCA_U32_SEL', 'u32_sel'),
               ('TCA_U32_POLICE', 'police'),
               ('TCA_U32_ACT', 'tca_act_prio'),
               ('TCA_U32_INDEV', 'hex'),
               ('TCA_U32_PCNT', 'u32_pcnt'),
               ('TCA_U32_MARK', 'u32_mark'))

    class tca_act_prio(nla):
        nla_map = tuple([('TCA_ACT_PRIO_%i' % x, 'tca_act') for x
                         in range(TCA_ACT_MAX_PRIO)])

        class tca_act(nla,
                      nla_plus_police,
                      nla_plus_tca_act_opt):
            nla_map = (('TCA_ACT_UNSPEC', 'none'),
                       ('TCA_ACT_KIND', 'asciiz'),
                       ('TCA_ACT_OPTIONS', 'get_act_options'),
                       ('TCA_ACT_INDEX', 'hex'),
                       ('TCA_ACT_STATS', 'get_stats2'))

            def get_stats2(self, *argv, **kwarg):
                return stats2

    class u32_sel(nla):
        fields = (('flags', 'B'),
                  ('offshift', 'B'),
                  ('nkeys', 'B'),
                  ('__align', 'B'),
                  ('offmask', '>H'),
                  ('off', 'H'),
                  ('offoff', 'h'),
                  ('hoff', 'h'),
                  ('hmask', '>I'))

        class u32_key(nlmsg):
            header = None
            fields = (('key_mask', '>I'),
                      ('key_val', '>I'),
                      ('key_off', 'i'),
                      ('key_offmask', 'i'))

        def encode(self):
            '''
            Key sample::

                'keys': ['0x0006/0x00ff+8',
                         '0x0000/0xffc0+2',
                         '0x5/0xf+0',
                         '0x10/0xff+33']

                => 00060000/00ff0000 + 8
                   05000000/0f00ffc0 + 0
                   00100000/00ff0000 + 32
            '''

            def cut_field(key, separator):
                '''
                split a field from the end of the string
                '''
                field = '0'
                pos = key.find(separator)
                new_key = key
                if pos > 0:
                    field = key[pos + 1:]
                    new_key = key[:pos]
                return (new_key, field)

            # 'header' array to pack keys to
            header = [(0, 0) for i in range(256)]

            keys = []
            # iterate keys and pack them to the 'header'
            for key in self['keys']:
                # TODO tags: filter
                (key, nh) = cut_field(key, '@')  # FIXME: do not ignore nh
                (key, offset) = cut_field(key, '+')
                offset = int(offset, 0)
                # a little trick: if you provide /00ff+8, that
                # really means /ff+9, so we should take it into
                # account
                (key, mask) = cut_field(key, '/')
                if mask[:2] == '0x':
                    mask = mask[2:]
                    while True:
                        if mask[:2] == '00':
                            offset += 1
                            mask = mask[2:]
                        else:
                            break
                    mask = '0x' + mask
                mask = int(mask, 0)
                value = int(key, 0)
                bits = 24
                if mask == 0 and value == 0:
                    key = self.u32_key(self.buf)
                    key['key_off'] = offset
                    key['key_mask'] = mask
                    key['key_val'] = value
                    keys.append(key)
                for bmask in struct.unpack('4B', struct.pack('>I', mask)):
                    if bmask > 0:
                        bvalue = (value & (bmask << bits)) >> bits
                        header[offset] = (bvalue, bmask)
                        offset += 1
                    bits -= 8

            # recalculate keys from 'header'
            key = None
            value = 0
            mask = 0
            for offset in range(256):
                (bvalue, bmask) = header[offset]
                if bmask > 0 and key is None:
                    key = self.u32_key(self.buf)
                    key['key_off'] = offset
                    key['key_mask'] = 0
                    key['key_val'] = 0
                    bits = 24
                if key is not None and bits >= 0:
                    key['key_mask'] |= bmask << bits
                    key['key_val'] |= bvalue << bits
                    bits -= 8
                    if (bits < 0 or offset == 255):
                        keys.append(key)
                        key = None

            assert keys
            self['nkeys'] = len(keys)
            # FIXME: do not hardcode flags :)
            self['flags'] = 1
            start = self.buf.tell()

            nla.encode(self)
            for key in keys:
                key.encode()
            self.update_length(start)

        def decode(self):
            nla.decode(self)
            self['keys'] = []
            nkeys = self['nkeys']
            while nkeys:
                key = self.u32_key(self.buf)
                key.decode()
                self['keys'].append(key)
                nkeys -= 1

    class u32_mark(nla):
        fields = (('val', 'I'),
                  ('mask', 'I'),
                  ('success', 'I'))

    class u32_pcnt(nla):
        fields = (('rcnt', 'Q'),
                  ('rhit', 'Q'),
                  ('kcnts', 'Q'))