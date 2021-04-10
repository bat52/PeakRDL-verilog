import os
import itertools

import jinja2 as jj
from systemrdl.node import RootNode, Node, RegNode, AddrmapNode, RegfileNode
from systemrdl.node import FieldNode, MemNode, AddressableNode
from systemrdl.rdltypes import AccessType, OnReadType, OnWriteType, PropertyReference
#from systemrdl import RDLWalker

class VerilogExporter:

    def __init__(self, **kwargs):
        """
        Constructor for the Verilog Exporter class

        Parameters
        ----------
        user_template_dir: str
            Path to a directory where user-defined template overrides are stored.
        user_template_context: dict
            Additional context variables to load into the template namespace.
        """
        user_template_dir = kwargs.pop("user_template_dir", None)
        self.user_template_context = kwargs.pop("user_template_context", dict())
        self.signal_overrides = {}
        self.strict = False # strict RDL rules rather than helpful impliciti behaviour

        # Check for stray kwargs
        if kwargs:
            raise TypeError("got an unexpected keyword argument '%s'" % list(kwargs.keys())[0])

        if user_template_dir:
            loader = jj.ChoiceLoader([
                jj.FileSystemLoader(user_template_dir),
                jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                jj.PrefixLoader({
                    'user': jj.FileSystemLoader(user_template_dir),
                    'base': jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))
                }, delimiter=":")
            ])
        else:
            loader = jj.ChoiceLoader([
                jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                jj.PrefixLoader({
                    'base': jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))
                }, delimiter=":")
            ])

        self.jj_env = jj.Environment(
            loader=loader,
            undefined=jj.StrictUndefined
        )

        # Define variables used during export
        RegNode.add_derived_property(self.bit_range)
        RegNode.add_derived_property(self.full_array_dimensions)
        RegNode.add_derived_property(self.full_array_ranges)
        RegNode.add_derived_property(self.full_array_indexes)
        RegNode.add_derived_property(self.has_intr)
        FieldNode.add_derived_property(self.is_hw_writable)
        FieldNode.add_derived_property(self.is_hw_readable)
        FieldNode.add_derived_property(self.is_up_counter)
        FieldNode.add_derived_property(self.is_down_counter)
        FieldNode.add_derived_property(self.bit_range)
        FieldNode.add_derived_property(self.full_array_ranges)
        FieldNode.add_derived_property(self.full_array_dimensions)
        FieldNode.add_derived_property(self.has_we)
        RegfileNode.add_derived_property(self.full_array_dimensions)
        RegfileNode.add_derived_property(self.full_array_ranges)

        # Top-level node
        self.top = None

        # Dictionary of group-like nodes (addrmap, regfile) and their bus
        # widths.
        # key = node path
        # value = max accesswidth/memwidth used in node's descendants
        self.bus_width_db = {}

        # Dictionary of root-level type definitions
        # key = definition type name
        # value = representative object
        #   components, this is the original_def (which can be None in some cases)
        self.namespace_db = {}


    def export(self, node: Node, path: str, **kwargs):
        """
        Perform the export!

        Parameters
        ----------
        node: systemrdl.Node
            Top-level node to export. Can be the top-level `RootNode` or any
            internal `AddrmapNode`.
        path: str
            Output file.
        signal_overrides: dict
            Mapping to override default signal suffixes , e.g. {'incr' : 'increment'}
        """
        self.signal_overrides = kwargs.pop("signal_overrides", dict())
        if type(self.signal_overrides) != dict:
            raise TypeError("got an unexpected signal_overrides argument of type {} instead of dict".format(
                                type(self.signal_overrides)))

        # Check for stray kwargs
        if kwargs:
            raise TypeError("got an unexpected keyword argument '%s'" % list(kwargs.keys())[0])

        # If it is the root node, skip to top addrmap
        if isinstance(node, RootNode):
            node = node.top


        # go through top level regfiles
        modules = []
        for desc in node.descendants():
            if ((isinstance(desc, (RegfileNode, RegNode))) and
                isinstance(desc.parent, AddrmapNode)):
                if desc.parent not in modules:
                    modules.append(desc.parent)

        for block in modules:
            print("Generating reg_block for {}".format(self._get_inst_name(block)))
            self.top = block


            # First, traverse the model and collect some information
            #self.bus_width_db = {}
            #RDLWalker().walk(self.top)

            context = {
                'print': print,
                'type': type,
                'top_node': block,
                'FieldNode': FieldNode,
                'RegNode': RegNode,
                'RegfileNode': RegfileNode,
                'AddrmapNode': AddrmapNode,
                'MemNode': MemNode,
                'AddressableNode': AddressableNode,
                'OnWriteType': OnWriteType,
                'OnReadType': OnReadType,
                'PropertyReference': PropertyReference,
                'isinstance': isinstance,
                'signal': self._get_signal_name,
                'full_idx': self._full_idx,
                'get_inst_name': self._get_inst_name,
                'get_field_access': self._get_field_access,
                'get_array_address_offset_expr': self._get_array_address_offset_expr,
                'get_bus_width': self._get_bus_width,
                'get_mem_access': self._get_mem_access,
                'roundup_to': self._roundup_to,
                'roundup_pow2': self._roundup_pow2,
                'get_prop_value': self._get_prop_value,
                'get_counter_value': self._get_counter_value,
            }

            context.update(self.user_template_context)

            template = self.jj_env.get_template("module.sv")
            stream = template.stream(context)
            stream.dump(os.path.join(path, node.inst_name + '_rf.sv'))
            template = self.jj_env.get_template("tb.sv")
            stream = template.stream(context)
            stream.dump(os.path.join(path, node.inst_name + '_tb.sv'))


    def _get_inst_name(self, node: Node) -> str:
        """
        Returns the class instance name
        """
        return node.inst_name


    def _get_field_access(self, field: FieldNode) -> str:
        """
        Get field's Verilog access string
        """
        sw = field.get_property("sw")
        onread = field.get_property("onread")
        onwrite = field.get_property("onwrite")

        if sw == AccessType.rw:
            if (onwrite is None) and (onread is None):
                return "RW"
            elif (onread == OnReadType.rclr) and (onwrite == OnWriteType.woset):
                return "W1SRC"
            elif (onread == OnReadType.rclr) and (onwrite == OnWriteType.wzs):
                return "W0SRC"
            elif (onread == OnReadType.rclr) and (onwrite == OnWriteType.wset):
                return "WSRC"
            elif (onread == OnReadType.rset) and (onwrite == OnWriteType.woclr):
                return "W1CRS"
            elif (onread == OnReadType.rset) and (onwrite == OnWriteType.wzc):
                return "W0CRS"
            elif (onread == OnReadType.rset) and (onwrite == OnWriteType.wclr):
                return "WCRS"
            elif onwrite == OnWriteType.woclr:
                return "W1C"
            elif onwrite == OnWriteType.woset:
                return "W1S"
            elif onwrite == OnWriteType.wot:
                return "W1T"
            elif onwrite == OnWriteType.wzc:
                return "W0C"
            elif onwrite == OnWriteType.wzs:
                return "W0S"
            elif onwrite == OnWriteType.wzt:
                return "W0T"
            elif onwrite == OnWriteType.wclr:
                return "WC"
            elif onwrite == OnWriteType.wset:
                return "WS"
            elif onread == OnReadType.rclr:
                return "WRC"
            elif onread == OnReadType.rset:
                return "WRS"
            else:
                return "RW"

        elif sw == AccessType.r:
            if onread is None:
                return "RO"
            elif onread == OnReadType.rclr:
                return "RC"
            elif onread == OnReadType.rset:
                return "RS"
            else:
                return "RO"

        elif sw == AccessType.w:
            if onwrite is None:
                return "WO"
            elif onwrite == OnWriteType.wclr:
                return "WOC"
            elif onwrite == OnWriteType.wset:
                return "WOS"
            else:
                return "WO"

        elif sw == AccessType.rw1:
            return "W1"

        elif sw == AccessType.w1:
            return "WO1"

        else: # na
            return "NOACCESS"


    def _get_mem_access(self, mem: MemNode) -> str:
        sw = mem.get_property("sw")
        if sw == AccessType.r:
            return "R"
        else:
            return "RW"


    def _get_array_address_offset_expr(self, node: AddressableNode) -> str:
        """
        Returns an expression to calculate the address offset
        for example, a 4-dimensional array allocated as:
            [A][B][C][D] @ X += Y
        results in:
            X + i0*B*C*D*Y + i1*C*D*Y + i2*D*Y + i3*Y
        """
        s = "'h%x" % node.raw_address_offset
        if node.is_array:
            for i in range(len(node.array_dimensions)):
                m = node.array_stride
                for j in range(i+1, len(node.array_dimensions)):
                    m *= node.array_dimensions[j]
                s += " + i%d*'h%x" % (i, m)
        return s


    def _get_signal_name(self, node: Node, index : str = '', prop : str = '') -> str:
        """
        Returns unique-in-addrmap name for signals
        """
        prefix = node.get_rel_path(node.owning_addrmap,'','_','','')

        # check for override, otherwise use prop
        suffix = self.signal_overrides.get(prop, prop)

        if prop:
            return "{}_{}{}".format(prefix, suffix, index)
        else:
            return "{}{}".format(prefix, index)


    def _get_bus_width(self, node: Node) -> int:
        """
        Returns group-like node's bus width (in bytes)
        """
        width = self.bus_width_db[node.get_path()]

        # Divide by 8, rounded up
        if width % 8:
            return width // 8 + 1
        else:
            return width // 8


    def _roundup_to(self, x: int, n: int) -> int:
        """
        Round x up to the nearest n
        """
        if x % n:
            return (x//n + 1) * n
        else:
            return (x//n) * n


    def _roundup_pow2(self, x):
        return 1<<(x-1).bit_length()


    # get property value where:
    #   true => hw input        (e.g. swwe, swwel)
    #   true => default value   (e.g. threshold)
    #   None => hw input        (e.g. incr, decr)
    #   None => default value   (e.g. incrvalue)
    def _get_prop_value(self, node, index, prop, hw_on_true=True, hw_on_none=False, default=0, width=1) -> str:
        """
        Converts the property value on the node to a SV value
        or signal for assignment and comparison
        """
        val = node.get_property(prop)

        # refactor negative defaults
        if default < 0:
            default = 2**width + default

        # DEUBUG: print(self._get_signal_name(node, '',''), prop, type(val), val, default)

        if isinstance(val, FieldNode):
            return self._get_signal_name(val, index, 'q')           # reference to field
        elif isinstance(val, PropertyReference):
            return self._get_signal_name(val.node, index, val.name) # reference to field property
        if type(val) == int:
            return "{}'d{}".format(width, val)                      # specified value
        elif ((val is True and hw_on_true) or
              (val is None and hw_on_none)):
            return self._get_signal_name(node, index, prop)         # hw input signal
        elif val is True or val is None:
            return "{}'d{}".format(width, default)                  # default value
        else:
            err = "ERROR: property {} of type {} not recognised".format(prop, type(val))
            print(err)
            return err


    def _get_counter_value(self, node, index, prop) -> str:
        """
        Returns the value or SV variable name for reference
        """
        val = node.get_property(prop+'value')
        width = node.get_property(prop+'width')

        if width:
            return self._get_signal_name(node, index, prop+'value')
        if not val:
            return 1 # default vlaue
        if type(val) == int:
            return val
        if val.parent != node.parent:
            print("ERROR: incrvalue reference only supported for fields in same reg")

        return self._get_signal_name(val, index, 'q')


    def is_up_counter(self, node) -> bool:
        """
        Field is an up counter
        """
        return (node.get_property('counter') and
                ( node.get_property('incrvalue') or
                  node.get_property('incrwidth') or
                  node.get_property('incr') or
                  not node.is_down_counter ) )


    def is_down_counter(self, node) -> bool:
        """
        Field is an up counter
        """
        return (node.get_property('counter') and
                ( node.get_property('decrvalue') or
                  node.get_property('decrwidth') or
                  node.get_property('decr') ) )


    def is_hw_writable(self, node) -> bool:
        """
        Field is writable by hardware
        """
        hw = node.get_property('hw')

        return hw in (AccessType.rw, AccessType.rw1,
                        AccessType.w, AccessType.w1)


    def is_hw_readable(self, node) -> bool:
        """
        Field is writable by hardware
        """
        hw = node.get_property('hw')

        return hw in (AccessType.rw, AccessType.rw1,
                        AccessType.r)


    def full_array_dimensions(self, node) -> list:
        """
        Get multi-dimensional array indexing for reg/field
        """
        if type(node) == AddrmapNode:
            return []
        else:
            return self.full_array_dimensions(node.parent) + (node.array_dimensions or [])


    def full_array_indexes(self, node) -> list:
        """
        Get multi-dimensional array indexing for reg/field
        """
        indexes = itertools.product(*[list(range(k)) for k in node.full_array_dimensions])
        return [''.join('[{}]'.format(k) for k in index) for index in indexes]


    def full_array_ranges(self, node, fmt='{:>20s}') -> str:
        """
        Get multi-dimensional array indexing for reg/field as SV ranges
        """
        return fmt.format(''.join('[{}:0]'.format(dim-1) for dim in node.full_array_dimensions))


    def _full_idx(self, node) -> str:
        """
        Get multi-dimensional array indexing for node instance
        """
        l = self._full_idx_list(node)
        return ''.join('[{}]'.format(k) for k in l)


    def _full_idx_list(self, node) -> list:
        """
        Get multi-dimensional array indexing for node instance
        """
        if type(node) == AddrmapNode:
            return []
        else:
            if node.is_array and not node.current_idx:
                print (node.get_path_segment(), node.current_idx)
            return self._full_idx_list(node.parent) + (list(node.current_idx or []))


    def bit_range(self, node, fmt='{msb:2}:{lsb:2}') -> str:
        """
        Formatted bit range for field
        """
        if type(node) == RegNode:
            return fmt.format(lsb=0, msb=node.size*8-1)
        else:
            return fmt.format(lsb=node.lsb, msb=node.msb)


    def has_intr(self, node : RegNode) -> bool:
        """
        Register has interrupt fields
        """
        for f in node.fields():
            if f.get_property('intr'):
                return True
        return False


    def has_we(self, node : FieldNode) -> bool:
        """
        Field has we input
        """
        if self.strict:
            # strict RDL says no we unless specified
            return node.get_property('we') == True

        if node.get_property('wel') != False:
            # can't have we and wel
            return False 

        return ((node.get_property('we') == True) or    # explicit
                (node.implements_storage and
                 node.is_hw_writable and
                 node.get_property('sticky') != True))  # storage without sticky unlikely to not want we